"""Controle d'acces de l'API.

La regle est celle de l'interface, pas une regle parallele : lire est ouvert a
tout compte authentifie, ecrire suppose `can_decide`. Une API qui appliquerait
une politique differente de celle des pages ferait de la seconde une decoration.
"""

from __future__ import annotations

from rest_framework import permissions

from apps.core.models import AuditLog
from apps.core.services import record_audit

MESSAGE = (
    "Votre role ne permet pas cette action. Elle modifie un dossier de "
    "candidature : elle est reservee aux comptes habilites, et journalisee."
)


class ReadOnlyOrCanDecide(permissions.BasePermission):
    """Lecture pour tous, ecriture pour les comptes habilites.

    Le refus est journalise, comme sur l'interface : une tentative refusee
    interesse un auditeur autant qu'une action reussie, et une API est
    justement l'endroit ou l'on tente.
    """

    message = MESSAGE

    def has_permission(self, request, view) -> bool:
        utilisateur = request.user
        if not (utilisateur and utilisateur.is_authenticated):
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        if getattr(utilisateur, "can_decide", False):
            return True

        record_audit(
            AuditLog.Action.STAGE_CHANGED,
            actor=utilisateur,
            summary=f"Action refusee : {request.path}",
            request=request,
            refus="role insuffisant",
            role=getattr(utilisateur, "role", ""),
            chemin=request.path,
            interface="api",
        )
        return False
