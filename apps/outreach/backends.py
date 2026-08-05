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
from email.mime.image import MIMEImage

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from apps.core import brand

from .exceptions import CanalNonConnecte, CoordonneeManquante, EnvoiBloque
from .models import Channel, Message

logger = logging.getLogger(__name__)


# En deca de cette longueur, une fin de ligne est voulue ; au-dela, c'est le
# gabarit texte qui a replie sa ligne. Voir `_lignes`.
SEUIL_REFLUX = 55


def _lignes(bloc: str) -> list[str]:
    """Rend a un paragraphe sa capacite a se recomposer.

    Les gabarits sont ecrits pour un courriel texte, donc replies autour de 75
    caracteres. Transformer chacun de ces retours en `<br>` figeait la coupure :
    le lecteur voyait « Votre profil a retenu notre attention » puis, a la
    ligne, « pour le poste de Data Engineer », quelle que soit la largeur de
    son ecran. Un paragraphe HTML doit se recomposer.

    Toutes les fins de ligne ne sont pas des replis pour autant — la signature
    en veut de vraies. Le depart se fait sur la longueur : une ligne pleine a
    ete repliee par le gabarit, une ligne courte a ete voulue courte. C'est le
    meme raisonnement que le format=flowed du courrier electronique, et il
    tient sur les gabarits du projet parce qu'ils sont tous replies a la meme
    largeur.
    """
    lignes: list[str] = []
    for ligne in (item.strip() for item in bloc.split("\n") if item.strip()):
        if lignes and len(lignes[-1]) >= SEUIL_REFLUX:
            lignes[-1] = f"{lignes[-1]} {ligne}"
        else:
            lignes.append(ligne)
    return lignes


def habiller(corps: str) -> str:
    """Enveloppe un texte deja redige dans la mise en page de la marque.

    Le decoupage en paragraphes se fait sur les lignes vides, comme le lecteur
    le comprend. Aucun mot n'est ajoute ni retire : la version HTML doit
    pouvoir etre comparee au texte enregistre sans surprise.
    """
    paragraphes = [
        _lignes(bloc) for bloc in corps.split("\n\n") if bloc.strip()
    ]
    return render_to_string(
        "outreach/email.html",
        {
            "paragraphes": paragraphes,
            "marque": {
                "cid": brand.CID_MARQUE,
                "racine": brand.NOM_RACINE,
                "suffixe": brand.NOM_SUFFIXE,
                "organisation": brand.organisation(),
                "encre": brand.ENCRE,
                "brand": brand.BRAND,
                "texte": brand.TEXTE,
                "attenue": brand.TEXTE_ATTENUE,
                "bordure": brand.BORDURE,
                "fond": brand.FOND,
                "surface": brand.SURFACE,
            },
        },
    )


def _marque_liee() -> MIMEImage:
    """La marque en piece jointe liee, referencee par `cid:` dans le HTML.

    `inline` plutot que `attachment` : sans cela le client affiche l'image dans
    le corps **et** un trombone, ce qui fait croire a une piece jointe reelle.
    """
    image = MIMEImage(brand.marque_png(taille=132, encre=brand.ENCRE))
    image.add_header("Content-ID", f"<{brand.CID_MARQUE}>")
    image.add_header("Content-Disposition", "inline", filename="recrutement-ia.png")
    return image


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
        courrier = EmailMultiAlternatives(
            subject=message.subject or f"Votre candidature — {message.application.offer.title}",
            body=message.body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            to=[adresse],
        )
        # Le texte reste la version de reference : c'est lui qui est enregistre,
        # relu et journalise. Le HTML n'en est qu'une presentation, construite
        # mecaniquement a partir du meme corps — un HTML qui dirait autre chose
        # rendrait le journal faux.
        courrier.attach_alternative(habiller(message.body), "text/html")
        courrier.mixed_subtype = "related"
        courrier.attach(_marque_liee())

        envoyes = courrier.send(fail_silently=False)
        return {"destinataire": adresse, "accepte": envoyes, "html": True}


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
