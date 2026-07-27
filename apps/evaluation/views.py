from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.matching.engine import ENGINE_VERSION

from . import bias, harness

DATASET = "ranking_v1"
# L'audit represente plusieurs centaines de scorings : c'est un calcul lourd,
# deterministe et rarement change. La cle inclut la version du moteur, de
# sorte qu'une evolution du scoring invalide le cache d'elle-meme.
CACHE_KEY = f"evaluation:report:{ENGINE_VERSION}:{DATASET}"
CACHE_SECONDS = 60 * 60


def _build_report() -> dict:
    standard, blind, mitigations = bias.compare_blind(DATASET)
    return {
        "report": standard,
        "blind_report": blind,
        "mitigations": mitigations,
        "neutralised": [item for item in mitigations if item.neutralised],
        "quality": harness.run(DATASET),
        "neutral": [d for d in standard.dimensions if not d.influences_score],
        "influential": [d for d in standard.dimensions if d.influences_score],
    }


class BiasReportView(LoginRequiredMixin, TemplateView):
    """Page de transparence : effet mesure des attributs identitaires.

    Exigence de l'AI Act pour un systeme a haut risque (annexe III.4) :
    l'exploitant doit pouvoir expliquer la logique de decision et documenter
    les mesures de reduction des biais. Cette page rend ces chiffres
    consultables par le recruteur lui-meme, pas seulement par le developpeur.
    """

    template_name = "evaluation/bias_report.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        data = cache.get(CACHE_KEY)
        if data is None:
            data = _build_report()
            cache.set(CACHE_KEY, data, CACHE_SECONDS)
            context["freshly_computed"] = True
        context.update(data)
        context["threshold"] = bias.IMPACT_RATIO_THRESHOLD
        context["engine_version"] = ENGINE_VERSION
        return context


class RefreshBiasReportView(LoginRequiredMixin, View):
    """Force un nouveau calcul, sans attendre l'expiration du cache."""

    def post(self, request):
        cache.delete(CACHE_KEY)
        return redirect(reverse("evaluation:bias_report"))
