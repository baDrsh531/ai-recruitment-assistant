from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.core.permissions import ActionPermissionMixin
from apps.matching.engine import ENGINE_VERSION

from . import (
    agreement,
    bias,
    harness,
    monitoring,
    report_pdf,
    search_eval,
    threshold,
)

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

        # Surveillance : le releve courant compare au dernier enregistre. On ne
        # journalise pas depuis un affichage — un controle enregistre a chaque
        # rafraichissement de page rendrait l'historique illisible.
        context["monitoring"] = monitoring.check(dataset_name=DATASET, record=False)
        context["monitoring_history"] = monitoring.historique()
        context["drift_threshold"] = monitoring.SEUIL_DERIVE
        return context


class RefreshBiasReportView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Force un nouveau calcul, sans attendre l'expiration du cache."""

    def post(self, request):
        cache.delete(CACHE_KEY)
        return redirect(reverse("evaluation:bias_report"))


class ThresholdView(LoginRequiredMixin, TemplateView):
    """Ou couper le classement, et ce que coute chaque choix de seuil.

    Le moteur classe ; il ne dit pas ou s'arreter. Cette page mesure ce que
    chaque seuil retient et surtout ce qu'il ecarte a tort, parce que c'est le
    chiffre que personne ne voit jamais dans un processus de recrutement.
    """

    template_name = "evaluation/threshold.html"

    def get_context_data(self, **kwargs):
        from apps.core import charts

        context = super().get_context_data(**kwargs)
        calibration = threshold.cached(DATASET)
        context["calibration"] = calibration
        context["curve"] = threshold.sampled_curve(calibration, step=0.05)
        context["engine_version"] = ENGINE_VERSION

        points = threshold.sampled_curve(calibration, step=0.10)
        # Les deux erreurs, pas les bons resultats : un graphique des profils
        # correctement retenus resterait plat sur les trois quarts de la plage
        # et ne montrerait aucun arbitrage. Ce sont les erreurs qui se croisent.
        context["chart"] = charts.grouped_bar(
            "chart-threshold",
            "Les deux erreurs, seuil par seuil",
            [
                (
                    f"{point.threshold_percentage} %",
                    point.false_positive,
                    point.false_negative,
                )
                for point in points
            ],
            ("Retenus a tort", "Bons profils manques"),
            unit="profils",
            kind="stack",
            subtitle="Le seuil retenu est celui qui minimise le total pondere",
            note=(
                "Un seuil bas retient trop de monde, un seuil haut ecarte des "
                "profils qu'il fallait recevoir. Les deux erreurs ne se valent "
                "pas : la seconde coute un recrutement, la premiere une heure "
                "d'entretien. C'est pourquoi le F2 pese le rappel quatre fois "
                "plus que la precision."
            ),
        )
        return context


class AgreementView(LoginRequiredMixin, TemplateView):
    """Accord entre recruteurs, et ecart au score.

    Le projet mesure beaucoup ce que fait le moteur et jamais ce que font les
    humains qui s'en servent. Cette page comble l'angle mort — sans noter
    personne : un recruteur qui s'ecarte du score peut avoir raison, il a vu le
    candidat quand le score a vu un PDF.
    """

    template_name = "evaluation/agreement.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["rapport"] = agreement.analyse()
        context["minimum"] = agreement.MIN_DOSSIERS_COMMUNS
        context["paliers"] = agreement.PALIERS
        return context


class ExportReportView(LoginRequiredMixin, View):
    """Rapport d'evaluation en PDF.

    L'export est ouvert en lecture — il ne modifie aucun dossier — mais il est
    journalise : un document qui sort du systeme est une donnee qui circule.
    """

    def get(self, request):
        donnees = cache.get(CACHE_KEY) or _build_report()
        calibration = threshold.cached(DATASET)

        # La recherche a son propre harnais ; s'il echoue, le rapport sort sans
        # cette section plutot que pas du tout.
        try:
            recherche = search_eval.run()
        except (FileNotFoundError, ValueError):
            recherche = None

        sources = report_pdf.Sources(
            quality=donnees["quality"],
            bias=donnees["report"],
            mitigations=donnees["mitigations"],
            calibration=calibration,
            search=recherche,
        )
        octets = report_pdf.build(sources, author=str(request.user))

        sections = ["classement", "biais", "seuil", "provenance"]
        if recherche is not None:
            sections.insert(3, "recherche")
        report_pdf.record_export(
            request.user, request=request, size=len(octets), sections=sections
        )

        reponse = HttpResponse(octets, content_type="application/pdf")
        reponse["Content-Disposition"] = (
            f'attachment; filename="{report_pdf.filename()}"'
        )
        return reponse


class InvocationDashboardView(LoginRequiredMixin, TemplateView):
    """Cout et latence des appels modele.

    Les donnees existent depuis le premier jour dans `AIInvocation` : latence,
    tokens, raisonnement actif, cause d'arret. Cette page les met en forme —
    c'est ce qui permet de repondre a « combien coute une extraction » autrement
    qu'a l'estime.
    """

    template_name = "evaluation/invocations.html"
    WINDOW_DAYS = 30

    def get_context_data(self, **kwargs):
        from collections import defaultdict
        from datetime import timedelta

        from django.db.models import Count, Sum
        from django.utils import timezone

        from apps.ai.models import AIInvocation
        from apps.core import charts

        context = super().get_context_data(**kwargs)
        depuis = timezone.now() - timedelta(days=self.WINDOW_DAYS)
        appels = AIInvocation.objects.filter(created_at__gte=depuis)

        latences: dict[str, list[float]] = defaultdict(list)
        for usage, latence in appels.filter(
            status=AIInvocation.Status.OK
        ).values_list("purpose", "latency_ms"):
            latences[usage].append(float(latence))

        totaux = appels.aggregate(
            total=Count("id"),
            entree=Sum("prompt_tokens"),
            sortie=Sum("completion_tokens"),
        )
        echecs = appels.filter(status=AIInvocation.Status.ERROR).count()
        toutes = [valeur for liste in latences.values() for valeur in liste]

        context["window_days"] = self.WINDOW_DAYS
        context["stats"] = {
            "calls": totaux["total"] or 0,
            "tokens": (totaux["entree"] or 0) + (totaux["sortie"] or 0),
            "median_ms": round(charts.percentile(toutes, 0.5)),
            "failures": echecs,
        }
        context["charts"] = {
            "latency": self._latency_chart(latences, charts),
            "tokens": self._tokens_chart(appels, charts),
            "volume": self._volume_chart(appels, charts),
        }
        return context

    # ----------------------------------------------------------------------
    def _latency_chart(self, latences, charts):
        lignes = [
            (
                usage,
                round(charts.percentile(valeurs, 0.5)),
                round(charts.percentile(valeurs, 0.95)),
            )
            for usage, valeurs in sorted(
                latences.items(), key=lambda item: -charts.percentile(item[1], 0.95)
            )
        ]
        return charts.grouped_bar(
            "chart-latency",
            "Latence par usage",
            lignes,
            ("Mediane", "95e centile"),
            unit="ms",
            subtitle="Appels reussis seulement",
            note=(
                "La mediane dit le regime courant, le 95e centile ce que subit "
                "l'utilisateur les mauvais jours. Les deux sur le meme axe : "
                "elles se comparent directement."
            ),
        )

    def _tokens_chart(self, appels, charts):
        from django.db.models import Sum

        lignes = [
            (
                row["purpose"],
                row["entree"] or 0,
                row["sortie"] or 0,
            )
            for row in appels.values("purpose")
            .annotate(entree=Sum("prompt_tokens"), sortie=Sum("completion_tokens"))
            .order_by("-sortie")
        ]
        return charts.grouped_bar(
            "chart-tokens",
            "Tokens consommes par usage",
            lignes,
            ("Entree", "Generes"),
            unit="tokens",
            kind="stack",
            subtitle=f"Cumul sur {self.WINDOW_DAYS} jours",
            note=(
                "Les tokens generes sont la partie couteuse. C'est ce poste qui "
                "avait motive la desactivation du raisonnement interne : seize "
                "fois plus de tokens pour un resultat identique."
            ),
        )

    def _volume_chart(self, appels, charts):
        from django.db.models import Count
        from django.db.models.functions import TruncDate

        lignes = (
            appels.annotate(jour=TruncDate("created_at"))
            .values("jour")
            .annotate(total=Count("id"))
            .order_by("jour")
        )
        return charts.line(
            "chart-volume",
            "Appels par jour",
            [(row["jour"].strftime("%d/%m"), row["total"]) for row in lignes],
            unit="appels",
            subtitle=f"{self.WINDOW_DAYS} derniers jours",
        )
