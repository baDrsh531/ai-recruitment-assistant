"""Contexte disponible dans tous les gabarits."""

from __future__ import annotations

from django.conf import settings


def demonstration(request) -> dict:
    """Etat de la demonstration publique.

    Ce que la demonstration ne peut pas montrer merite d'etre dit. Les deux
    modeles tournent sur un serveur d'inference prive, injoignable depuis
    l'exterieur : l'analyse redigee et les questions d'entretien y sont donc
    indisponibles. Sans cette mention, un visiteur conclurait que la
    fonctionnalite est cassee — alors que le moteur, lui, n'a jamais eu besoin
    d'un modele pour calculer un score.
    """
    return {
        "demo_mode": settings.DEMO_MODE,
        "llm_disponible": bool(getattr(settings, "LLM_BASE_URL", "")),
    }
