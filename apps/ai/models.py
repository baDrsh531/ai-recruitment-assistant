"""Tracabilite des appels au serveur d'inference."""

from django.db import models

from apps.core.models import BaseModel


class AIInvocation(BaseModel):
    """Un appel modele = une ligne.

    Alimente trois choses a la fois :
      - le journal d'audit (quel modele, quelle version de prompt, quand) ;
      - le tableau de bord de cout et de latence (p50/p95, tokens) ;
      - la reproductibilite : `input_hash` permet de rejouer un resultat.
    """

    class Kind(models.TextChoices):
        CHAT = "chat", "Texte"
        VISION = "vision", "Vision"
        EMBEDDING = "embedding", "Embedding"

    class Status(models.TextChoices):
        OK = "ok", "Succes"
        ERROR = "error", "Erreur"

    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.CHAT)
    purpose = models.CharField("usage", max_length=64, db_index=True)
    prompt_id = models.CharField(max_length=64, blank=True)
    prompt_version = models.CharField(max_length=16, blank=True)
    model = models.CharField(max_length=120)
    base_url = models.CharField(max_length=200, blank=True)

    input_hash = models.CharField(max_length=64, db_index=True)
    temperature = models.FloatField(default=0.0)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=1)

    # Raisonnement interne actif ? Sur Qwen3.6, l'activer multiplie par seize
    # le nombre de tokens generes pour une extraction structuree : la question
    # merite d'etre suivie appel par appel.
    thinking = models.BooleanField("raisonnement actif", default=False)
    finish_reason = models.CharField(max_length=24, blank=True)

    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OK)
    error = models.TextField(blank=True)

    # Lien souple vers l'objet metier concerne (candidature, CV, offre...).
    subject_type = models.CharField(max_length=64, blank=True)
    subject_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        verbose_name = "appel IA"
        verbose_name_plural = "appels IA"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["purpose", "-created_at"]),
            models.Index(fields=["subject_type", "subject_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} · {self.model} · {self.latency_ms} ms"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
