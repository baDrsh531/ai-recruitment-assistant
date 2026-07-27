"""Resultats de scoring, conserves comme un historique.

Un score n'est jamais ecrase : chaque calcul cree une ligne. On peut donc
rejouer une decision six mois plus tard et savoir quel moteur, quels poids et
quelle version de prompt l'ont produite — exigence de tracabilite pour un
systeme d'IA a haut risque.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.candidates.models import Application
from apps.core.models import BaseModel


class MatchScore(BaseModel):
    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="scores"
    )

    overall = models.FloatField(
        "score global",
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        db_index=True,
    )
    engine_version = models.CharField(max_length=16)
    weights_used = models.JSONField(default=dict)
    breakdown = models.JSONField(default=dict)
    skill_matches = models.JSONField(default=list)
    gaps = models.JSONField(default=list)
    semantic_used = models.BooleanField(
        "rapprochement semantique disponible", default=False
    )
    blind = models.BooleanField(
        "calcule en screening a l'aveugle",
        default=False,
        help_text="Localisation exclue du calcul, employeurs masques dans l'analyse.",
    )
    compute_ms = models.PositiveIntegerField(default=0)

    # Analyse redigee par le modele a partir du score deja calcule.
    explanation = models.TextField(blank=True)
    explanation_prompt_id = models.CharField(max_length=64, blank=True)
    explanation_prompt_version = models.CharField(max_length=16, blank=True)
    explanation_model = models.CharField(max_length=120, blank=True)

    # Correction manuelle : le recruteur garde la main sur le classement.
    overridden_score = models.FloatField(
        "score corrige", null=True, blank=True,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="score_overrides",
    )
    override_reason = models.TextField(blank=True)

    class Meta:
        verbose_name = "score de compatibilite"
        verbose_name_plural = "scores de compatibilite"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["application", "-created_at"]),
            models.Index(fields=["-overall"]),
        ]

    def __str__(self) -> str:
        return f"{self.application} — {self.percentage} %"

    @property
    def effective_score(self) -> float:
        """Score retenu pour le classement : la correction humaine prime."""
        return self.overridden_score if self.overridden_score is not None else self.overall

    @property
    def percentage(self) -> int:
        return round(self.effective_score * 100)

    @property
    def is_overridden(self) -> bool:
        return self.overridden_score is not None

    @property
    def criteria(self) -> list[dict]:
        return self.breakdown.get("criteria", [])

    @property
    def applicable_criteria(self) -> list[dict]:
        return [criterion for criterion in self.criteria if criterion.get("applicable")]

    @property
    def skipped_criteria(self) -> list[dict]:
        return [criterion for criterion in self.criteria if not criterion.get("applicable")]

    @property
    def matched_skills(self) -> list[dict]:
        return [match for match in self.skill_matches if match.get("score", 0) >= 0.5]
