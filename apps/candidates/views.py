from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count
from django.views.generic import DetailView, ListView, TemplateView

from apps.ai.models import AIInvocation
from apps.core import charts
from apps.core.models import AuditLog
from apps.jobs.models import JobOffer
from apps.matching import services
from apps.matching.models import MatchScore

from . import retention
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
            "skills": self._skills_chart(),
            "experience": self._experience_chart(),
            "languages": self._languages_chart(),
            "scores": self._scores_chart(),
        }

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
        # Chaque calcul cree une ligne : on ne retient que le plus recent par
        # candidature, sans quoi un recalcul comptrait deux fois.
        derniers: dict[str, float] = {}
        for score in MatchScore.objects.order_by("application_id", "-created_at"):
            derniers.setdefault(str(score.application_id), score.effective_score)

        valeurs = list(derniers.values())
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
        return Candidate.objects.prefetch_related("skills").order_by("full_name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["blind"] = self.request.user.blind_screening
        return context


class CandidateDetailView(LoginRequiredMixin, DetailView):
    model = Candidate
    template_name = "candidates/candidate_detail.html"
    context_object_name = "candidate"

    def get_queryset(self):
        return Candidate.objects.prefetch_related(
            "skills", "experiences", "education", "languages", "certifications", "documents"
        )


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
        return context
