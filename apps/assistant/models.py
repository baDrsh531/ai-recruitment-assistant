"""Journal des questions posees a l'assistant.

Chaque question est conservee avec les filtres qu'elle a produits, les
candidats retenus et la reponse redigee. Deux raisons : un recruteur doit
pouvoir revenir sur ce qui lui a ete montre, et la question « pourquoi ce
candidat est-il remonte ? » doit avoir une reponse verifiable six mois plus
tard.
"""

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel
from apps.jobs.models import JobOffer


class RecruiterQuery(BaseModel):
    offer = models.ForeignKey(
        JobOffer, on_delete=models.CASCADE, related_name="recruiter_queries"
    )
    asked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="recruiter_queries",
    )

    question = models.TextField()
    # Criteres produits par le modele, tels qu'appliques ensuite par le code.
    filters = models.JSONField(default=dict)
    # Criteres discriminatoires ecartes de la question, s'il y en avait.
    rejected_criteria = models.JSONField(default=list, blank=True)
    matched_ids = models.JSONField(default=list, blank=True)
    matched_count = models.PositiveIntegerField(default=0)

    answer = models.TextField(blank=True)
    filter_prompt_version = models.CharField(max_length=16, blank=True)
    answer_prompt_version = models.CharField(max_length=16, blank=True)
    model = models.CharField(max_length=200, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "question de recruteur"
        verbose_name_plural = "questions de recruteur"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["offer", "-created_at"])]

    def __str__(self) -> str:
        return self.question[:80]

    @property
    def has_rejected_criteria(self) -> bool:
        return bool(self.rejected_criteria)
