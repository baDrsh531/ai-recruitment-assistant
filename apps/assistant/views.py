from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView

from apps.ai.client import InferenceError
from apps.candidates.models import Candidate
from apps.core.permissions import ActionPermissionMixin
from apps.jobs.models import JobOffer

from .filters import FilterSet
from .models import RecruiterQuery
from .service import ask

# Questions proposees en un clic : elles montrent ce que l'assistant sait faire,
# y compris refuser un critere discriminatoire.
SUGGESTIONS = [
    "Qui maitrise Python et Django avec au moins deux ans d'experience ?",
    "Quels candidats parlent francais et anglais ?",
    "Qui connait Django mais pas React ?",
    "Montre-moi les trois meilleurs profils.",
]


class AssistantView(LoginRequiredMixin, DetailView):
    model = JobOffer
    template_name = "assistant/assistant.html"
    context_object_name = "offer"
    slug_url_kwarg = "slug"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        historique = list(self.object.recruiter_queries.all()[:10])
        context["history"] = historique
        context["suggestions"] = SUGGESTIONS
        context["blind"] = self.request.user.blind_screening

        derniere = historique[0] if historique else None
        context["latest"] = derniere
        context["matched"] = (
            _dans_l_ordre(derniere.matched_ids) if derniere else []
        )
        # Les criteres sont affiches en francais, pas dans la syntaxe brute du
        # dictionnaire : `skills_all ['Python']` ne se lit pas.
        context["criteria_summary"] = (
            FilterSet.from_payload(derniere.filters).summary() if derniere else []
        )
        return context


class AskView(ActionPermissionMixin, LoginRequiredMixin, View):
    def post(self, request, slug):
        offer = get_object_or_404(JobOffer, slug=slug)
        question = request.POST.get("question", "")

        try:
            ask(offer, question, actor=request.user, request=request)
        except ValueError:
            messages.error(request, "Posez une question avant d'envoyer.")
        except InferenceError as exc:
            messages.error(
                request,
                "La question n'a pas pu etre traduite en criteres : "
                f"le serveur d'inference n'a pas repondu. {exc}",
            )
        return redirect("assistant:offer", slug=offer.slug)


class ClearHistoryView(ActionPermissionMixin, LoginRequiredMixin, View):
    def post(self, request, slug):
        offer = get_object_or_404(JobOffer, slug=slug)
        RecruiterQuery.objects.filter(offer=offer).delete()
        messages.success(request, "Historique des questions efface.")
        return redirect("assistant:offer", slug=offer.slug)


def _dans_l_ordre(identifiants: list[str]) -> list[Candidate]:
    """Recharge les candidats en conservant l'ordre du resultat."""
    if not identifiants:
        return []
    trouves = {
        str(candidate.pk): candidate
        for candidate in Candidate.objects.filter(pk__in=identifiants).prefetch_related(
            "skills"
        )
    }
    return [trouves[identifiant] for identifiant in identifiants if identifiant in trouves]
