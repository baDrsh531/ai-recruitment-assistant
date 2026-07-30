from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.ai.models import AIInvocation
from apps.assistant import textsearch
from apps.core import charts
from apps.core.models import AuditLog
from apps.core.permissions import ActionPermissionMixin
from apps.core.services import record_audit
from apps.evaluation import report_pdf, threshold
from apps.jobs.models import JobOffer
from apps.matching import counterfactual, services
from apps.matching.models import MatchScore

from . import duplicates, retention
from .models import Application, Candidate, CandidateLanguage, CandidateSkill, CVDocument

# Tranches d'anciennete, dans un ordre qui porte du sens : elles ne sont pas
# retriees par effectif.
EXPERIENCE_BANDS = (
    ("Moins d'un an", 0.0, 1.0),
    ("1 a 3 ans", 1.0, 3.0),
    ("3 a 5 ans", 3.0, 5.0),
    ("5 a 8 ans", 5.0, 8.0),
    ("8 ans et plus", 8.0, float("inf")),
)

SCORE_BANDS = (
    ("Moins de 25 %", 0.0, 0.25),
    ("25 a 50 %", 0.25, 0.50),
    ("50 a 75 %", 0.50, 0.75),
    ("75 % et plus", 0.75, 1.01),
)


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stats"] = {
            "candidates": Candidate.objects.count(),
            "open_offers": JobOffer.objects.filter(status=JobOffer.Status.OPEN).count(),
            "applications": Application.objects.count(),
            "documents_pending": CVDocument.objects.exclude(
                status=CVDocument.Status.DONE
            ).count(),
        }
        context["ai"] = AIInvocation.objects.aggregate(
            calls=Count("id"), avg_latency=Avg("latency_ms")
        )
        context["stages"] = [
            {
                "stage": dict(Application.Stage.choices).get(row["stage"], row["stage"]),
                "total": row["total"],
            }
            for row in Application.objects.values("stage")
            .annotate(total=Count("id"))
            .order_by("-total")
        ]
        context["recent_offers"] = JobOffer.objects.order_by("-created_at")[:5]
        context["charts"] = self._charts()
        context["retention"] = {
            "days": settings.DATA_RETENTION_DAYS,
            "window": retention.WARNING_WINDOW_DAYS,
            "expiring": retention.expiring_soon().count(),
            "expired": retention.expired().count(),
        }
        return context

    # ----------------------------------------------------------------------
    def _charts(self) -> dict[str, charts.Chart]:
        return {
            "decided": self._decided_ring(),
            "shortlist": self._shortlist_ring(),
            "retention": self._retention_ring(),
            "skills": self._skills_chart(),
            "experience": self._experience_chart(),
            "languages": self._languages_chart(),
            "scores": self._scores_chart(),
        }

    # --- Jauges : un ratio, un chiffre ------------------------------------
    def _decided_ring(self) -> charts.Chart:
        total = Application.objects.count()
        decidees = Application.objects.exclude(
            stage=Application.Stage.RECEIVED
        ).count()
        return charts.ring(
            "ring-decided",
            "Candidatures traitees",
            decidees,
            total,
            label="Dossiers ayant recu une decision",
            unit="candidatures",
            subtitle="Sorties de l'etat « recue »",
            note=(
                "Une candidature qui reste en « recue » n'a pas ete refusee : "
                "elle n'a pas ete regardee. C'est le seul chiffre de cette page "
                "qui mesure le travail du recruteur et non celui du moteur."
            ),
        )

    def _shortlist_ring(self) -> charts.Chart:
        derniers = self._latest_scores()
        seuil = threshold.recommended_threshold()
        au_dessus = sum(1 for valeur in derniers if valeur >= seuil)
        return charts.ring(
            "ring-shortlist",
            "Au-dessus du seuil mesure",
            au_dessus,
            len(derniers),
            label="Candidatures au-dessus du seuil",
            unit="candidatures",
            subtitle=f"Seuil calibre a {round(seuil * 100)} %",
            note=(
                "La ligne marque le classement, elle n'ecarte personne. Un "
                "dossier sous le seuil reste consultable, scorable et recevable."
            ),
        )

    def _retention_ring(self) -> charts.Chart:
        total = Candidate.objects.count()
        return charts.ring(
            "ring-retention",
            "Conservation des dossiers",
            retention.expired().count(),
            total,
            label="Dossiers arrives a echeance",
            unit="dossiers",
            subtitle=f"Duree de conservation : {settings.DATA_RETENTION_DAYS} jours",
            target=0.0,
            target_label="objectif : aucun dossier echu",
            invert=True,
            note=(
                "Ici, zero est le bon resultat : un dossier echu est un dossier "
                "que la purge quotidienne aurait du supprimer."
            ),
        )

    def _latest_scores(self) -> list[float]:
        """Dernier score de chaque candidature. Un recalcul ne compte pas deux fois."""
        derniers: dict[str, float] = {}
        for score in MatchScore.objects.order_by("application_id", "-created_at"):
            derniers.setdefault(str(score.application_id), score.effective_score)
        return list(derniers.values())

    def _skills_chart(self) -> charts.Chart:
        rows = (
            CandidateSkill.objects.values("normalized_name")
            .annotate(total=Count("candidate", distinct=True))
            .order_by("-total")
        )
        return charts.bar(
            "chart-skills",
            "Competences les plus repandues",
            [(row["normalized_name"], row["total"]) for row in rows],
            other_label="autres competences",
            unit="candidats",
            subtitle="Nombre de candidats declarant la competence",
            note=(
                "Les intitules sont normalises par l'ontologie : « DRF » et "
                "« Django REST Framework » comptent pour une seule competence."
            ),
        )

    def _experience_chart(self) -> charts.Chart:
        annees = list(
            Candidate.objects.values_list("total_experience_years", flat=True)
        )
        pairs = [
            (label, sum(1 for valeur in annees if bas <= valeur < haut))
            for label, bas, haut in EXPERIENCE_BANDS
        ]
        return charts.ordered_bar(
            "chart-experience",
            "Anciennete des candidats",
            pairs,
            unit="candidats",
            subtitle="Tranches ordonnees, effectif par tranche",
            note=(
                "L'anciennete totale fusionne les periodes qui se chevauchent : "
                "deux missions menees en parallele ne comptent qu'une fois."
            ),
        )

    def _languages_chart(self) -> charts.Chart:
        rows = (
            CandidateLanguage.objects.values("language")
            .annotate(total=Count("candidate", distinct=True))
            .order_by("-total")
        )
        return charts.bar(
            "chart-languages",
            "Langues parlees",
            [(row["language"], row["total"]) for row in rows],
            unit="candidats",
            subtitle="Toutes candidatures confondues",
        )

    def _scores_chart(self) -> charts.Chart:
        valeurs = self._latest_scores()
        pairs = [
            (label, sum(1 for valeur in valeurs if bas <= valeur < haut))
            for label, bas, haut in SCORE_BANDS
        ]
        return charts.ordered_bar(
            "chart-scores",
            "Distribution des scores",
            pairs,
            unit="candidatures",
            subtitle="Dernier score calcule pour chaque candidature",
            note=(
                "Le score est une aide au tri. Aucune candidature n'est ecartee "
                "automatiquement, quelle que soit sa tranche."
            ),
        )


class CandidateListView(LoginRequiredMixin, ListView):
    model = Candidate
    template_name = "candidates/candidate_list.html"
    context_object_name = "candidates"
    paginate_by = 25

    def get_queryset(self):
        requete = self.request.GET.get("q", "").strip()
        if not requete:
            return Candidate.objects.prefetch_related("skills").order_by("full_name")

        # Recherche plein texte : l'ordre vient du score BM25, pas du nom. On
        # ne repasse pas par la base pour trier, sans quoi le classement
        # obtenu serait perdu.
        self.search_result = textsearch.search(requete, limit=50)
        return [hit.candidate for hit in self.search_result.hits]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["blind"] = self.request.user.blind_screening
        context["query"] = self.request.GET.get("q", "").strip()
        context["search"] = getattr(self, "search_result", None)
        # Le gabarit calculait ce total avec `paginator.count|default:…|length`.
        # Or `length` applique a un entier renvoie 0 : le compteur affichait
        # « 0 candidat(s) » des qu'il y en avait, et n'etait juste que sur une
        # base vide. Le total se calcule ici, ou il est simplement disponible.
        paginateur = context.get("paginator")
        context["total"] = (
            paginateur.count if paginateur else len(context["candidates"])
        )
        return context


class CandidateDetailView(LoginRequiredMixin, DetailView):
    model = Candidate
    template_name = "candidates/candidate_detail.html"
    context_object_name = "candidate"

    def get_queryset(self):
        return Candidate.objects.prefetch_related(
            "skills", "experiences", "education", "languages", "certifications", "documents"
        )


class DuplicateListView(LoginRequiredMixin, TemplateView):
    """Dossiers susceptibles de designer la meme personne.

    La page propose, elle ne fusionne pas : le rapprochement est une hypothese,
    et deux homonymes sont des gens differents.
    """

    template_name = "candidates/duplicates.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        groupes = duplicates.scan()
        context["groups"] = groupes
        context["can_decide"] = self.request.user.can_decide
        context["blind"] = self.request.user.blind_screening
        context["threshold"] = duplicates.SEUIL
        context["stats"] = {
            "groups": len(groupes),
            "records": sum(groupe.size for groupe in groupes),
            "certain": sum(1 for groupe in groupes if groupe.confidence >= 0.9),
        }
        context["merges"] = AuditLog.objects.filter(
            action=AuditLog.Action.CANDIDATES_MERGED
        ).select_related("actor")[:10]
        return context


class MergeCandidatesView(ActionPermissionMixin, LoginRequiredMixin, View):
    """Fusionne des dossiers, sur decision d'un recruteur habilite."""

    def post(self, request):
        garde = Candidate.objects.filter(pk=request.POST.get("keep")).first()
        autres = list(Candidate.objects.filter(pk__in=request.POST.getlist("merge")))

        if garde is None:
            messages.error(request, "Dossier a conserver introuvable.")
            return redirect("candidates:duplicates")

        try:
            duplicates.merge(garde, autres, actor=request.user, request=request)
        except duplicates.MergeRefused as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"{len(autres) + 1} dossiers fusionnes dans « {garde.full_name} ». "
                "L'operation est journalisee et definitive.",
            )
        return redirect("candidates:duplicates")


class ExportApplicationView(LoginRequiredMixin, View):
    """Dossier d'une candidature en PDF.

    Le rapport de transparence dit ce que vaut le systeme ; celui-ci dit ce qui
    a ete fait d'un candidat. C'est le document qu'un candidat peut demander au
    titre de l'article 15 du RGPD, et celui qu'un recruteur emporte en entretien.
    L'export est journalise : un dossier qui sort du systeme circule.
    """

    def get(self, request, pk):
        candidature = get_object_or_404(
            Application.objects.select_related("candidate", "offer"), pk=pk
        )
        octets = report_pdf.build_application(
            candidature,
            score=candidature.scores.order_by("-created_at").first(),
            decisions=AuditLog.objects.filter(
                action=AuditLog.Action.STAGE_CHANGED,
                object_type="Application",
                object_id=str(candidature.pk),
            ).select_related("actor"),
            questions=list(candidature.interview_questions.all()),
            author=str(request.user),
            blind=request.user.blind_screening,
        )

        record_audit(
            AuditLog.Action.DATA_EXPORTED,
            actor=request.user,
            obj=candidature,
            summary=f"Dossier exporte ({len(octets) // 1024} Ko)",
            request=request,
            format="pdf",
            scope="candidature",
            blind=request.user.blind_screening,
            bytes=len(octets),
        )

        reponse = HttpResponse(octets, content_type="application/pdf")
        reponse["Content-Disposition"] = (
            f'attachment; filename="{report_pdf.application_filename(candidature)}"'
        )
        return reponse


class ApplicationDetailView(LoginRequiredMixin, DetailView):
    model = Application
    template_name = "candidates/application_detail.html"
    context_object_name = "application"

    def get_queryset(self):
        return Application.objects.select_related("candidate", "offer", "document")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # L'historique des scores est conserve : on affiche le plus recent.
        context["score"] = self.object.scores.order_by("-created_at").first()
        context["questions"] = list(self.object.interview_questions.all())
        context["stages"] = Application.Stage.choices
        context["can_decide"] = self.request.user.can_decide
        context["stages_requiring_note"] = list(services.STAGES_REQUIRING_NOTE)
        # Trace des decisions successives : le champ du modele ne garde que la
        # derniere, le journal d'audit garde toutes les precedentes.
        context["decisions"] = AuditLog.objects.filter(
            action=AuditLog.Action.STAGE_CHANGED,
            object_type="Application",
            object_id=str(self.object.pk),
        ).select_related("actor")[:10]
        # Ce qu'il manque pour atteindre le seuil. Le calcul rejoue le moteur
        # une quarantaine de fois ; mesure sur le pire cas du jeu de
        # demonstration : 14 ms et 7 requetes, soit moins qu'un score commente.
        #
        # Le seuil vise est celui que la calibration recommande, pas un chiffre
        # rond : demander « ce qu'il manque pour atteindre 75 % » quand la coupe
        # utile se situe ailleurs repondrait a cote de la question.
        context["counterfactual"] = counterfactual.analyse(
            self.object.candidate,
            self.object.offer,
            target=threshold.recommended_threshold(),
        )
        return context
