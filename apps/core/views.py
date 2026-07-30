"""Sonde de sante.

Un hebergeur a besoin de savoir si le service repond avant de basculer le
trafic dessus. Repondre 200 sur la page d'accueil ne suffit pas : elle exige une
session, et un 302 vers la connexion ressemble a un service sain alors que la
base peut etre injoignable. La sonde touche donc la base, et elle seule.
"""

from __future__ import annotations

from django.db import connection
from django.http import JsonResponse

from apps.matching.engine import ENGINE_VERSION


def sante(request):
    """Etat du service. Publique, sans donnee metier."""
    try:
        with connection.cursor() as curseur:
            curseur.execute("SELECT 1")
            curseur.fetchone()
    except Exception as exc:  # noqa: BLE001
        return JsonResponse(
            {"status": "degrade", "base": "injoignable", "detail": str(exc)[:200]},
            status=503,
        )

    return JsonResponse({"status": "ok", "base": "ok", "moteur": ENGINE_VERSION})
