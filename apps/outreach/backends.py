"""Transport des messages : ce qui part vraiment, et ce qui ne part pas.

Un seul canal est reellement connecte : l'e-mail, par la couche courriel de
Django — console en developpement, SMTP en production, sans une ligne de code
specifique.

**WhatsApp et le SMS ne sont pas connectes, et ce module le dit au lieu de le
simuler.** Les brancher demande un compte WhatsApp Business, des gabarits
valides par Meta et un numero verifie ; un operateur pour le SMS. Rien de tout
cela n'existe ici. Ecrire un faux expediteur qui journalise « envoye » aurait
donne une demonstration plus flatteuse et un systeme qui ment : le jour ou les
identifiants arrivent, personne ne saurait plus quels messages sont reellement
partis.

Ce que le projet apporte donc pour ces canaux, c'est le modele de donnees, le
consentement, le journal et l'interface d'expedition — tout sauf le cable. Y
brancher un fournisseur est l'affaire d'une classe qui implemente `expedier`.

Sur une demonstration publique, **tous** les canaux sont fermes, e-mail
compris. Une demonstration en ligne qui expedie de vrais courriers a des
adresses saisies par des inconnus est un incident, pas une fonctionnalite.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMessage

from .exceptions import CanalNonConnecte, CoordonneeManquante, EnvoiBloque
from .models import Channel, Message

logger = logging.getLogger(__name__)


class Expediteur:
    """Interface d'un canal. `expedier` part ou leve, jamais entre les deux."""

    canal: str = ""

    def destinataire(self, message: Message) -> str:
        raise NotImplementedError

    def expedier(self, message: Message) -> dict:
        raise NotImplementedError


class ExpediteurEmail(Expediteur):
    canal = Channel.EMAIL

    def destinataire(self, message: Message) -> str:
        adresse = (message.application.candidate.email or "").strip()
        if not adresse:
            raise CoordonneeManquante(
                "Ce candidat n'a pas d'adresse e-mail dans son dossier. "
                "L'extraction ne l'a pas trouvee sur le CV, ou le CV n'en "
                "portait pas."
            )
        return adresse

    def expedier(self, message: Message) -> dict:
        adresse = self.destinataire(message)
        courrier = EmailMessage(
            subject=message.subject or f"Votre candidature — {message.application.offer.title}",
            body=message.body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            to=[adresse],
        )
        envoyes = courrier.send(fail_silently=False)
        return {"destinataire": adresse, "accepte": envoyes}


class ExpediteurNonConnecte(Expediteur):
    """Canal modelise, journalise, consenti — mais sans fournisseur derriere."""

    def __init__(self, canal: str, prerequis: str) -> None:
        self.canal = canal
        self.prerequis = prerequis

    def destinataire(self, message: Message) -> str:
        numero = (message.application.candidate.phone or "").strip()
        if not numero:
            raise CoordonneeManquante(
                "Ce candidat n'a pas de numero de telephone dans son dossier."
            )
        return numero

    def expedier(self, message: Message) -> dict:
        # Le numero est verifie d'abord : un recruteur doit apprendre les deux
        # problemes, pas seulement le premier.
        self.destinataire(message)
        raise CanalNonConnecte(
            f"Le canal {self.canal} n'est pas connecte a un fournisseur. "
            f"{self.prerequis} Le message reste en brouillon ; il peut etre "
            f"copie et envoye a la main, puis consigne dans le journal."
        )


EXPEDITEURS: dict[str, Expediteur] = {
    Channel.EMAIL: ExpediteurEmail(),
    Channel.WHATSAPP: ExpediteurNonConnecte(
        Channel.WHATSAPP,
        "Il faudrait un compte WhatsApp Business, un numero verifie et des "
        "gabarits valides par Meta.",
    ),
    Channel.SMS: ExpediteurNonConnecte(
        Channel.SMS, "Il faudrait un contrat avec un operateur d'envoi."
    ),
}


def hors_logiciel(canal: str) -> bool:
    """Canal qu'aucun fournisseur ne pourra jamais brancher.

    Un appel telephonique ne se connecte pas : il se passe, puis il se
    consigne. Le ranger avec WhatsApp — non connecte mais connectable —
    laisserait croire qu'il manque une integration a ecrire, alors qu'il ne
    manque rien.
    """
    return canal in {Channel.CALL, Channel.OTHER}


def canal_connecte(canal: str) -> bool:
    expediteur = EXPEDITEURS.get(canal)
    return expediteur is not None and not isinstance(expediteur, ExpediteurNonConnecte)


def etat_du_canal(canal: str) -> str:
    """Trois etats distincts, parce qu'ils appellent trois conduites."""
    if hors_logiciel(canal):
        return "hors_logiciel"
    return "connecte" if canal_connecte(canal) else "connectable"


def expedier(message: Message) -> dict:
    """Fait partir un message. Leve `EnvoiRefuse` si ce n'est pas possible."""
    if getattr(settings, "DEMO_MODE", False):
        raise EnvoiBloque(
            "Envoi ferme : cette instance est une demonstration publique. Les "
            "adresses saisies ici appartiennent a des personnes qui n'ont rien "
            "demande — le brouillon se redige et se relit, il ne part pas."
        )

    expediteur = EXPEDITEURS.get(message.channel)
    if expediteur is None:
        raise CanalNonConnecte(
            f"Aucun expediteur pour le canal « {message.channel} »."
        )
    return expediteur.expedier(message)
