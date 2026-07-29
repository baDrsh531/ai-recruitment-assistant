"""Controle d'acces aux actions qui modifient un dossier.

Le modele utilisateur declarait `can_decide` depuis l'origine, avec un
commentaire affirmant que seul un humain habilite tranche. Il n'etait appele
nulle part : un compte en lecture seule pouvait relancer un scoring, generer
des questions, deposer un CV et faire avancer une candidature. Une propriete
qui n'est jamais verifiee ne protege rien.

La regle est unique et volontairement simple : **toute action qui ecrit passe
par `can_decide`**. Consulter reste ouvert a tous les comptes ; agir suppose
d'en repondre, puisque chaque action est imputee a son auteur dans le journal
d'audit.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .models import AuditLog
from .services import record_audit

logger = logging.getLogger(__name__)

REFUS = (
    "Votre role ne permet pas cette action. Elle modifie un dossier de "
    "candidature : elle est reservee aux comptes habilites, et journalisee."
)


class ActionPermissionMixin:
    """Refuse aux comptes en lecture seule toute action modifiant un dossier.

    Le refus est journalise : une tentative d'action non autorisee est une
    information utile a un auditeur, pas seulement une erreur d'interface.
    """

    permission_denied_message = REFUS

    def dispatch(self, request, *args, **kwargs):
        utilisateur = request.user
        if utilisateur.is_authenticated and not utilisateur.can_decide:
            record_audit(
                AuditLog.Action.STAGE_CHANGED,
                actor=utilisateur,
                summary=f"Action refusee : {request.path}",
                request=request,
                refus="role insuffisant",
                role=utilisateur.role,
                chemin=request.path,
            )
            logger.info(
                "Action refusee a %s (role %s) sur %s",
                utilisateur, utilisateur.role, request.path,
            )
            return self.handle_no_permission_for_role(request)
        return super().dispatch(request, *args, **kwargs)

    def handle_no_permission_for_role(self, request):
        """Renvoie l'utilisateur d'ou il vient avec une explication.

        Une action refusee vaut mieux qu'une page d'erreur : le compte a le
        droit de consulter, il n'a pas celui d'ecrire.
        """
        messages.error(request, self.permission_denied_message)
        retour = request.META.get("HTTP_REFERER")
        if retour:
            return redirect(retour)
        raise PermissionDenied(self.permission_denied_message)
