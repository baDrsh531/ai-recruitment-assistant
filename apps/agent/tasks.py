"""Declenchement de l'agent, en tache de fond ou en synchrone.

Le projet tourne sans broker en developpement : Celery s'execute alors en
**synchrone**, et un `.delay()` bloque l'appelant. Un agent qui enchaine quatre
etapes dont deux appellent un modele bloquerait la requete HTTP de depot d'un
CV pendant une minute — l'utilisateur croirait a un plantage.

`declencher()` regarde donc s'il y a un broker avant de choisir. Avec broker :
le depot rend la main immediatement. Sans : rien n'est declenche a chaud, et le
travail attend la commande `run_agent`. Ne rien faire est ici le bon
comportement — mieux vaut un traitement differe qu'une page qui se fige.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


def broker_disponible() -> bool:
    """Un broker est-il configure ?

    `CELERY_BROKER_URL` vide signifie mode synchrone : Celery execute la tache
    dans le processus appelant.
    """
    return bool(getattr(settings, "CELERY_BROKER_URL", "") or "")


@shared_task
def run_agent_task(application_id: str | None = None, trigger: str = "schedule") -> dict:
    """Execution de l'agent. Renvoie de quoi lire le resultat dans un journal."""
    from apps.candidates.models import Application

    from . import pipeline

    lot = None
    if application_id:
        lot = Application.objects.filter(pk=application_id).select_related(
            "candidate", "offer"
        )

    resultat = pipeline.run(applications=lot, trigger=trigger)
    execution = resultat.run
    return {
        "run": str(execution.pk),
        "status": execution.status,
        "processed": execution.applications_processed,
        "recommendations": execution.recommendations_made,
        "failed_steps": execution.steps_failed,
    }


def declencher(application=None, *, trigger: str = "upload") -> bool:
    """Lance l'agent en tache de fond si c'est possible.

    Renvoie vrai si le travail a ete confie a un broker. Faux signifie que rien
    n'a ete lance — volontairement — et que la commande `run_agent` s'en
    chargera.
    """
    from . import budget

    if not budget.agent_actif():
        return False
    if not broker_disponible():
        logger.info(
            "Agent : aucun broker, le dossier attendra la commande run_agent."
        )
        return False

    run_agent_task.delay(
        str(application.pk) if application is not None else None, trigger
    )
    return True
