"""Depot d'un CV et declenchement de l'extraction."""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile

from apps.candidates.models import Application, CVDocument
from apps.core.models import AuditLog
from apps.core.services import record_audit, sha256_of
from apps.jobs.models import JobOffer

logger = logging.getLogger(__name__)


def validate_upload(upload: UploadedFile) -> None:
    suffix = Path(upload.name).suffix.lower()
    if suffix not in settings.ALLOWED_CV_EXTENSIONS:
        allowed = ", ".join(settings.ALLOWED_CV_EXTENSIONS)
        raise ValidationError(f"Format non pris en charge ({suffix}). Attendu : {allowed}.")
    if upload.size > settings.MAX_CV_SIZE_BYTES:
        limit = settings.MAX_CV_SIZE_BYTES // (1024 * 1024)
        raise ValidationError(f"Fichier trop volumineux (limite : {limit} Mo).")


def ingest(
    upload: UploadedFile,
    *,
    offer: JobOffer | None = None,
    actor=None,
    request=None,
) -> tuple[CVDocument, bool]:
    """Enregistre un CV et lance son extraction.

    Renvoie (document, cree). Le hash du contenu sert de cle : redeposer un
    fichier deja traite reutilise l'extraction existante au lieu de refaire
    tourner le modele.
    """
    validate_upload(upload)
    content_hash = sha256_of(upload)

    existing = CVDocument.objects.filter(content_hash=content_hash).first()
    if existing:
        logger.info("CV deja connu (%s), extraction reutilisee", content_hash[:12])
        _attach_to_offer(existing, offer, actor)
        return existing, False

    document = CVDocument(
        original_filename=upload.name[:255],
        content_hash=content_hash,
        size_bytes=upload.size,
    )
    document.file.save(Path(upload.name).name, upload, save=False)
    document.save()

    record_audit(
        AuditLog.Action.CV_UPLOADED,
        actor=actor,
        obj=document,
        summary=upload.name,
        request=request,
        offer=str(offer.pk) if offer else None,
        size_bytes=upload.size,
    )

    from .tasks import parse_document_task

    parse_document_task.delay(str(document.pk), str(actor.pk) if actor else None)

    document.refresh_from_db()
    _attach_to_offer(document, offer, actor)
    return document, True


def _attach_to_offer(document: CVDocument, offer: JobOffer | None, actor) -> None:
    if offer is None or document.candidate_id is None:
        return
    Application.objects.get_or_create(candidate=document.candidate, offer=offer, defaults={
        "document": document,
    })
