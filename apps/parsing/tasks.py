"""Taches asynchrones d'extraction.

Sans broker Celery configure, `CELERY_TASK_ALWAYS_EAGER` execute ces taches en
synchrone : le projet reste fonctionnel en developpement sans Redis.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from apps.ai.client import InferenceError
from apps.candidates.models import CVDocument

logger = logging.getLogger(__name__)


# On ne rejoue que les erreurs transitoires (serveur d'inference indisponible).
# Un PDF corrompu ou un format non pris en charge echouera a l'identique a
# chaque tentative : le rejouer ne ferait que retarder le signalement.
@shared_task(
    bind=True,
    autoretry_for=(InferenceError,),
    retry_backoff=10,
    retry_kwargs={"max_retries": 2},
    acks_late=True,
)
def parse_document_task(self, document_id: str, actor_id: str | None = None):
    """Extrait un CV. Les erreurs sont deja tracees sur le document lui-meme."""
    from .pipeline import parse_document

    document = CVDocument.objects.get(pk=document_id)
    actor = None
    if actor_id:
        actor = get_user_model().objects.filter(pk=actor_id).first()

    candidate = parse_document(document, actor=actor)
    logger.info("CV %s extrait -> candidat %s", document_id, candidate.pk)
    return str(candidate.pk)
