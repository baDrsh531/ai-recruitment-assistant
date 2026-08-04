"""Agent d'orchestration : ce qu'il a fait, et ce qu'il propose.

Deux modeles, et la separation entre les deux est le coeur du dispositif.

`AgentRun` enregistre une execution : ce qui a ete traite, ce que ca a coute,
ce qui a echoue. C'est la reponse a « qu'a fait la machine cette semaine, et
combien ». Un agent qui tourne sans laisser ce releve est ingerable.

`Recommendation` porte une decision **proposee**, jamais prise. Elle reste en
attente jusqu'a ce qu'un recruteur la valide ou la rejette — et c'est sa
validation a lui qui devient la decision journalisee, pas la proposition de
l'agent. La nuance n'est pas rhetorique : elle determine qui repond de la
decision devant un candidat.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.candidates.models import Application
from apps.core.models import BaseModel


class AgentRun(BaseModel):
    """Une execution de l'agent."""

    class Status(models.TextChoices):
        RUNNING = "running", "En cours"
        DONE = "done", "Terminee"
        FAILED = "failed", "Echouee"
        # Le budget est une limite dure, pas un avertissement : une execution
        # arretee par le plafond est un etat distinct d'un echec.
        BUDGET = "budget", "Arretee par le budget"
        DISABLED = "disabled", "Agent desactive"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Lancee a la main"
        UPLOAD = "upload", "Depot d'un CV"
        SCHEDULE = "schedule", "Tache periodique"

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="agent_runs",
    )

    applications_seen = models.PositiveIntegerField("dossiers examines", default=0)
    applications_processed = models.PositiveIntegerField("dossiers traites", default=0)
    steps_done = models.PositiveIntegerField("etapes executees", default=0)
    steps_failed = models.PositiveIntegerField("etapes en echec", default=0)
    recommendations_made = models.PositiveIntegerField("recommandations", default=0)

    tokens_used = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "execution de l'agent"
        verbose_name_plural = "executions de l'agent"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["-created_at"]), models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"{self.get_status_display()} — {self.applications_processed} dossier(s)"

    @property
    def failed_ratio(self) -> float:
        total = self.steps_done + self.steps_failed
        return self.steps_failed / total if total else 0.0

    @property
    def ok(self) -> bool:
        return self.status == self.Status.DONE


class Recommendation(BaseModel):
    """Une decision proposee par l'agent, en attente d'un humain.

    Elle ne fait rien avancer par elle-meme. Tant qu'un recruteur ne l'a pas
    tranchee, la candidature reste ou elle est.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "En attente"
        ACCEPTED = "accepted", "Suivie"
        REJECTED = "rejected", "Ecartee par le recruteur"
        # Le score a change depuis : la proposition ne porte plus sur les
        # memes chiffres et ne doit plus etre presentee comme valable.
        STALE = "stale", "Perimee"

    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="recommendations"
    )
    run = models.ForeignKey(
        AgentRun, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="recommendations",
    )

    proposed_stage = models.CharField("etape proposee", max_length=20)
    rationale = models.TextField("motif propose")
    # Score sur lequel la proposition a ete faite. Sert a la perimer si le
    # dossier est recalcule.
    score_at_time = models.FloatField(default=0.0)
    threshold_at_time = models.FloatField(default=0.0)
    engine_version = models.CharField(max_length=16, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="agent_recommendations",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)

    class Meta:
        verbose_name = "recommandation"
        verbose_name_plural = "recommandations"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["application", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.application} -> {self.proposed_stage} ({self.get_status_display()})"

    @property
    def pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def percentage(self) -> int:
        return round(self.score_at_time * 100)

    @property
    def proposes_rejection(self) -> bool:
        from apps.matching.services import STAGES_REQUIRING_NOTE

        return self.proposed_stage in STAGES_REQUIRING_NOTE
