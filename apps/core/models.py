"""Modeles de base partages par toutes les applications."""

import uuid

from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("cree le", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("modifie le", auto_now=True)

    class Meta:
        abstract = True


class BaseModel(TimeStampedModel):
    """Cle primaire UUID : les identifiants candidats ne fuitent pas de volumetrie."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """Journal d'audit immuable.

    Exigence AI Act (Annexe III.4 : systeme a haut risque) : toute decision
    assistee par IA doit etre tracable a posteriori — qui, quand, quel modele,
    quelle version de prompt, quel resultat.
    """

    class Action(models.TextChoices):
        CV_UPLOADED = "cv_uploaded", "CV depose"
        CV_PARSED = "cv_parsed", "CV analyse"
        SCORE_COMPUTED = "score_computed", "Score calcule"
        SCORE_OVERRIDDEN = "score_overridden", "Score corrige manuellement"
        STAGE_CHANGED = "stage_changed", "Etape modifiee"
        CANDIDATE_VIEWED = "candidate_viewed", "Candidat consulte"
        DATA_EXPORTED = "data_exported", "Donnees exportees"
        DATA_PURGED = "data_purged", "Donnees purgees"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    summary = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "entree d'audit"
        verbose_name_plural = "journal d'audit"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.get_action_display()}"

    def save(self, *args, **kwargs):
        if self.pk and AuditLog.objects.filter(pk=self.pk).exists():
            raise ValueError("Le journal d'audit est immuable : modification interdite.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Le journal d'audit est immuable : suppression interdite.")
