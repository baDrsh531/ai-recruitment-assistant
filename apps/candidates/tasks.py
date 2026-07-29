"""Taches periodiques liees a la conservation des donnees."""

from __future__ import annotations

import logging

from celery import shared_task

from . import retention

logger = logging.getLogger(__name__)


@shared_task
def purge_expired_task() -> int:
    """Purge quotidienne des dossiers arrives a echeance.

    A programmer une fois par jour. Sans broker configure, la tache reste
    appelable a la main — et la commande `purge_expired` fait la meme chose.
    """
    rapport = retention.purge()
    if rapport.deleted:
        logger.info("Purge RGPD quotidienne : %s dossier(s)", rapport.deleted)
    return rapport.deleted
