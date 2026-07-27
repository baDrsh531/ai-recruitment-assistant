"""Taches asynchrones de scoring."""

from __future__ import annotations

import logging

from celery import shared_task

from apps.candidates.models import Application
from apps.jobs.models import JobOffer

from .services import score_application, score_offer

logger = logging.getLogger(__name__)


@shared_task
def score_application_task(application_id: str, with_explanation: bool = True) -> str:
    application = Application.objects.select_related("candidate", "offer").get(
        pk=application_id
    )
    score = score_application(application, with_explanation=with_explanation)
    return str(score.pk)


@shared_task
def score_offer_task(offer_id: str, with_explanation: bool = True) -> int:
    offer = JobOffer.objects.get(pk=offer_id)
    scores = score_offer(offer, with_explanation=with_explanation)
    logger.info("Offre %s : %s candidatures scorees", offer.title, len(scores))
    return len(scores)
