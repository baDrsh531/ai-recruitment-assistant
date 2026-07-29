from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView

from apps.ai.client import InferenceError
from apps.candidates.models import Application, Candidate
from apps.jobs.models import JobOffer

from . import comparison, engine, interview
from .services import latest_scores, score_offer


class OfferRankingView(LoginRequiredMixin, DetailView):
    model = JobOffer
    template_name = "matching/ranking.html"
    context_object_name = "offer"
    slug_url_kwarg = "slug"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scores = latest_scores(self.object)
        context["scores"] = scores
        context["blind"] = self.request.user.blind_screening
        context["unscored"] = (
            self.object.applications.count()
            - len({score.application_id for score in scores})
        )
        return context


class ScoreOfferView(LoginRequiredMixin, View):
    """Relance le calcul pour toutes les candidatures d'une offre."""

    def post(self, request, slug):
        offer = get_object_or_404(JobOffer, slug=slug)
        explain = request.POST.get("explain") == "1"

        try:
            scores = score_offer(offer, with_explanation=explain, actor=request.user)
        except InferenceError as exc:
            messages.warning(
                request,
                f"Scores calcules, mais l'analyse redigee est indisponible : {exc}",
            )
            scores = latest_scores(offer)
        else:
            degraded = [score for score in scores if not score.semantic_used]
            if scores and degraded:
                messages.info(
                    request,
                    "Rapprochement semantique indisponible : seules les "
                    "correspondances exactes et l'ontologie ont ete utilisees.",
                )
            messages.success(request, f"{len(scores)} candidature(s) scoree(s).")

        return redirect("matching:ranking", slug=offer.slug)


class ComparisonView(LoginRequiredMixin, DetailView):
    """Comparaison de plusieurs candidats sur une meme offre.

    Sans selection explicite, les trois premiers du classement sont compares :
    c'est la question que se pose le recruteur juste apres avoir trie.
    """

    model = JobOffer
    template_name = "matching/comparison.html"
    context_object_name = "offer"
    slug_url_kwarg = "slug"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        classement = latest_scores(self.object)

        demandes = self.request.GET.getlist("c")
        if demandes:
            choisis = list(
                Candidate.objects.filter(
                    pk__in=demandes, applications__offer=self.object
                ).distinct()
            )
            # On respecte l'ordre du classement, pas celui de l'URL.
            rang = {str(s.application.candidate_id): i for i, s in enumerate(classement)}
            choisis.sort(key=lambda c: rang.get(str(c.pk), 999))
        else:
            choisis = [score.application.candidate for score in classement]

        choisis = choisis[: comparison.MAX_CANDIDATES]
        context["selection"] = [str(candidate.pk) for candidate in choisis]
        context["ranking"] = classement
        context["max_candidates"] = comparison.MAX_CANDIDATES
        context["blind"] = self.request.user.blind_screening
        context["comparison"] = comparison.compare(self.object, choisis) if choisis else None
        return context


class GenerateQuestionsView(LoginRequiredMixin, View):
    """Genere les questions d'entretien d'une candidature."""

    def post(self, request, pk):
        application = get_object_or_404(
            Application.objects.select_related("candidate", "offer"), pk=pk
        )
        result = engine.score(application.candidate, application.offer)

        try:
            questions = interview.generate(application, result, actor=request.user)
        except InferenceError as exc:
            messages.error(request, f"Generation impossible : {exc}")
        else:
            messages.success(
                request, f"{len(questions)} question(s) d'entretien generees."
            )
        return redirect("candidates:application_detail", pk=application.pk)
