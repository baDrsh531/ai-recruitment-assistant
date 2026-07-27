from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView

from apps.ai.client import InferenceError
from apps.jobs.models import JobOffer

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
