from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.core.models import AuditLog
from apps.core.permissions import ActionPermissionMixin
from apps.matching.engine import ENGINE_VERSION

from . import (
    agreement,
    bias,
    harness,
    monitoring,
    replay,
    report_pdf,
    search_eval,
    threshold,
    variance,
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


class ReplayView(LoginRequiredMixin, TemplateView):
    """Les decisions passees, recalculees avec le moteur d'aujourd'hui.

    Le projet affirme que le score est reproductible. Cette page cesse de
    l'affirmer et le verifie sur les vraies decisions.
    """

    template_name = "evaluation/replay.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rapport = replay.rejouer(limit=200)
        context["rapport"] = rapport
        # Ce qui merite d'etre lu en premier : ce qui aurait bascule, puis ce
        # qui a bouge, puis ce qu'on ne peut pas trancher.
        context["a_regarder"] = sorted(
            rapport.bascules + rapport.divergents + rapport.non_concluants,
            key=lambda item: (not item.bascule, -abs(item.ecart)),
        )[:30]
        context["tolerance_points"] = round(replay.TOLERANCE * 100, 3)
        return context


class VarianceView(LoginRequiredMixin, TemplateView):
    """Ce que le modele fait varier, et ce qu'il ne touche jamais.

    La mesure appelle le modele plusieurs fois : elle ne part **jamais au
    chargement**, seulement sur une action explicite. Une page qui consommerait
    des tokens a chaque visite serait une facture qui court toute seule.
    """

    template_name = "evaluation/variance.html"

    def get_context_data(self, **kwargs):
        from apps.candidates.models import Application

        context = super().get_context_data(**kwargs)
        context["candidatures"] = (
            Application.objects.filter(scores__isnull=False)
            .select_related("candidate", "offer")
            .distinct()[:40]
        )
        context["mesure"] = self.request.session.pop("variance", None)
        return context


class RunVarianceView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Lance la mesure. Le resultat transite par la session, pas par l'URL."""

    def post(self, request):
        from django.contrib import messages as flash

        from apps.candidates.models import Application

        candidature = Application.objects.filter(
            pk=request.POST.get("candidature")
        ).select_related("candidate", "offer").first()
        if candidature is None:
            flash.error(request, "Candidature introuvable.")
            return redirect("evaluation:variance")

        try:
            tirages = max(2, min(5, int(request.POST.get("tirages", 3))))
        except ValueError:
            tirages = 3

        mesure = variance.mesurer(candidature, tirages=tirages)
        request.session["variance"] = {
            "candidat": candidature.candidate.full_name,
            "offre": candidature.offer.title,
            "score": round(mesure.score, 4),
            "nombre": mesure.nombre,
            "recouvrement": mesure.recouvrement_median,
            "longueurs": mesure.longueurs,
            "amplitude": mesure.ecart_de_longueur,
            "attendus": sorted(mesure.chiffres_attendus),
            "cites": [tirage.pourcentages_cites for tirage in mesure.tirages],
            "inventes": mesure.chiffres_inventes,
            "fidele": mesure.fidele,
            "lecture": mesure.lecture,
            "indisponible": mesure.indisponible,
            "textes": [tirage.texte for tirage in mesure.tirages],
        }
        return redirect("evaluation:variance")


class AuditTrailView(LoginRequiredMixin, TemplateView):
    """Le journal d'audit, enfin consultable.

    Le modele existait depuis l'origine, immuable et complet — et aucune page
    ne l'affichait. Pour un systeme classe a haut risque, « montrez-moi tout ce
    qui est arrive a ce candidat » est la premiere demande d'un auditeur comme
    d'un candidat exercant son droit d'acces. Un journal qu'on ne peut pas lire
    ne prouve rien.
    """

    template_name = "evaluation/audit_trail.html"
    PAR_PAGE = 60

    def get_context_data(self, **kwargs):
        from django.contrib.auth import get_user_model
        from django.core.paginator import Paginator
        from django.db.models import Q

        context = super().get_context_data(**kwargs)
        entrees = AuditLog.objects.select_related("actor")

        action = self.request.GET.get("action", "")
        acteur = self.request.GET.get("acteur", "")
        objet = self.request.GET.get("objet", "")
        recherche = self.request.GET.get("q", "").strip()
        machine = self.request.GET.get("machine", "")

        if action:
            entrees = entrees.filter(action=action)
        if acteur:
            entrees = entrees.filter(actor_id=acteur)
        if objet:
            entrees = entrees.filter(object_id=objet)
        if recherche:
            entrees = entrees.filter(
                Q(summary__icontains=recherche) | Q(object_id__icontains=recherche)
            )
        # Separer la machine de l'humain sans avoir a lire les metadonnees :
        # c'est la premiere chose qu'un auditeur veut distinguer.
        #
        # `exclude(metadata__agent=True)` ne convient pas : sur une entree ou
        # la cle est absente, la comparaison vaut NULL, sa negation vaut NULL,
        # et la ligne disparait. Le filtre « humain seul » ne renvoyait alors
        # rien — c'est-a-dire l'inverse de ce qu'il annonce. On passe donc par
        # l'ensemble des identifiants marques.
        if machine in ("0", "1"):
            marquees = AuditLog.objects.filter(metadata__agent=True).values("pk")
            entrees = (
                entrees.filter(pk__in=marquees)
                if machine == "1"
                else entrees.exclude(pk__in=marquees)
            )

        pages = Paginator(entrees, self.PAR_PAGE)
        page = pages.get_page(self.request.GET.get("page"))

        context["page"] = page
        context["actions"] = AuditLog.Action.choices
        context["acteurs"] = get_user_model().objects.order_by("username")
        context["filtres"] = {
            "action": action, "acteur": acteur, "objet": objet,
            "q": recherche, "machine": machine,
        }
        context["actifs"] = any([action, acteur, objet, recherche, machine])
        context["total"] = pages.count
        context["total_general"] = AuditLog.objects.count()
        return context


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
