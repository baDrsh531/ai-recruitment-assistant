"""Ce qu'on a le droit d'ecrire a un candidat, et ce qu'on en garde.

Trois regles portent le module.

**Le consentement se verifie a l'envoi, pas a la redaction.** Un recruteur peut
preparer un message WhatsApp pour un candidat qui n'a rien autorise ; il ne
peut pas l'expedier. Bloquer la redaction aurait cache le probleme au lieu de
le poser : le brouillon existe, le refus explique ce qui manque, et enregistrer
l'accord debloque l'envoi sans reecrire le texte.

**Le modele de langage ne redige rien seul.** Il personnalise un gabarit deja
valide (voir `drafting.py`). Un message part donc toujours d'un texte qu'un
humain a pu relire dans sa forme generique.

**Un message envoye ne se modifie plus.** Corriger apres coup le texte d'un
courrier qu'une personne a deja lu transformerait le journal en fiction. On en
redige un autre.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.candidates.models import Application, Candidate
from apps.core import brand
from apps.core.models import AuditLog
from apps.core.services import record_audit

from . import backends, drafting, registry, salutation
from .exceptions import ConsentementManquant, EnvoiRefuse, MessageDejaParti
from .models import CANAUX_SUR_ACCORD, Channel, Consent, Message

logger = logging.getLogger(__name__)

# Canaux dont le format impose un texte court.
CANAUX_COURTS = frozenset({Channel.SMS, Channel.WHATSAPP})


# --- Consentement ------------------------------------------------------------
def dernier_consentement(candidate: Candidate, channel: str) -> Consent | None:
    """Dernier enregistrement pour ce canal. Les precedents restent en base.

    **En cas d'egalite de date, le refus l'emporte.** L'horloge de Windows
    avance par paliers d'environ 15 ms : deux enregistrements poses dans le
    meme tic portent la meme date, et la cle primaire etant un UUID, rien ne
    dit lequel est arrive en second. Trier sur la seule date rendait donc le
    resultat aleatoire — un retrait enregistre juste apres un accord pouvait ne
    pas prendre effet, et le systeme aurait ecrit a quelqu'un qui venait de
    demander le contraire.

    Departager vers le refus est le seul sens defendable : quand on ne sait
    pas, on n'ecrit pas.
    """
    dernier = (
        Consent.objects.filter(candidate=candidate, channel=channel)
        .order_by("-created_at")
        .first()
    )
    if dernier is None:
        return None

    refus = (
        Consent.objects.filter(
            candidate=candidate,
            channel=channel,
            created_at=dernier.created_at,
            granted=False,
        )
        .order_by("-created_at")
        .first()
    )
    return refus or dernier


def autorise(candidate: Candidate, channel: str) -> bool:
    """A-t-on le droit d'ecrire a ce candidat sur ce canal ?

    Un accord explicite tranche toujours, dans les deux sens : il autorise un
    canal presume interdit, et un retrait ferme un canal presume ouvert. C'est
    le second cas qui compte le plus — un candidat qui demande a ne plus etre
    appele doit etre entendu meme si l'appel etait justifie par sa candidature.
    """
    enregistre = dernier_consentement(candidate, channel)
    if enregistre is not None:
        return enregistre.granted
    return channel not in CANAUX_SUR_ACCORD


def enregistrer_consentement(
    candidate: Candidate,
    *,
    channel: str,
    granted: bool,
    actor=None,
    source: str = Consent.Source.VERBAL,
    note: str = "",
    request=None,
) -> Consent:
    """Ajoute un enregistrement. N'ecrase jamais le precedent."""
    consentement = Consent.objects.create(
        candidate=candidate,
        channel=channel,
        granted=granted,
        source=source,
        recorded_by=actor if getattr(actor, "is_authenticated", False) else None,
        note=note.strip(),
    )
    record_audit(
        AuditLog.Action.CONSENT_RECORDED,
        actor=actor,
        obj=candidate,
        summary=(
            f"{'Accord' if granted else 'Refus'} de contact par "
            f"{consentement.get_channel_display()}"
        ),
        request=request,
        channel=channel,
        granted=granted,
        source=source,
    )
    return consentement


def canaux_ouverts(candidate: Candidate) -> list[dict]:
    """Etat de chaque canal pour ce candidat, tel qu'il s'affiche."""
    etat = []
    for channel, libelle in Channel.choices:
        if channel == Channel.OTHER:
            continue
        enregistre = dernier_consentement(candidate, channel)
        etat.append(
            {
                "channel": channel,
                "libelle": libelle,
                "autorise": autorise(candidate, channel),
                "explicite": enregistre is not None,
                "sur_accord": channel in CANAUX_SUR_ACCORD,
                "connecte": backends.canal_connecte(channel),
                "etat": backends.etat_du_canal(channel),
                "enregistre": enregistre,
            }
        )
    return etat


# --- Redaction ---------------------------------------------------------------
def _salutation(candidate: Candidate, *, blind: bool) -> str:
    """« Bonjour Sara, » ou « Bonjour, ».

    En screening a l'aveugle, la formule reste neutre. Le message part bien a
    la bonne adresse — c'est le transport qui la lit, pas le recruteur. Ce qui
    disparait, c'est le prenom a l'ecran de celui qui redige : l'afficher pour
    l'occasion aurait rouvert, au moment d'ecrire, ce que l'attenuation du
    biais ferme au moment d'evaluer.

    Hors mode aveugle, `salutation.formule` renonce quand l'ordre du nom est
    indechiffrable, plutot que de risquer « Bonjour EL, ».
    """
    return salutation.formule(candidate.full_name or "", blind=blind)


def valeurs_par_defaut(application: Application, *, actor=None, blind: bool = False) -> dict:
    """Valeurs injectees dans les gabarits."""
    signataire = ""
    if actor is not None and getattr(actor, "is_authenticated", False):
        signataire = actor.get_full_name() or actor.get_username()

    return {
        "salutation": _salutation(application.candidate, blind=blind),
        "poste": application.offer.title,
        "entreprise": brand.organisation(),
        "signataire": signataire or "l'equipe recrutement",
        "delai": getattr(settings, "OUTREACH_RESPONSE_DAYS", 15),
        "retention": getattr(settings, "DATA_RETENTION_DAYS", 365),
        "duree": 45,
        "question": "Pourriez-vous nous preciser ce point ?",
        "motif": "votre profil ne correspond pas aux attendus du poste.",
        # Volontairement vague : le gabarit n'invente ni salaire, ni date, ni
        # statut. Ce que le recruteur n'a pas saisi n'est pas annonce.
        "conditions": (
            "Nous reviendrons vers vous tres vite avec les modalites pratiques "
            "— date de demarrage, conditions et rattachement."
        ),
    }


def rediger(
    application: Application,
    *,
    modele_id: str,
    channel: str = Channel.EMAIL,
    actor=None,
    avec_modele: bool = True,
    blind: bool | None = None,
    **surcharges,
) -> Message:
    """Prepare un brouillon. N'envoie rien, ne verifie aucun consentement.

    Le brouillon existe meme sur un canal ferme : c'est en tentant de l'envoyer
    qu'on apprend ce qui manque, et le texte n'est alors pas perdu.
    """
    modele = registry.get(modele_id)
    if not modele.accepte(channel):
        raise ValueError(
            f"Le modele « {modele.libelle} » ne s'envoie pas par "
            f"{Channel(channel).label}."
        )

    if blind is None:
        blind = bool(getattr(actor, "blind_screening", False))

    valeurs = valeurs_par_defaut(application, actor=actor, blind=blind)
    valeurs.update({cle: valeur for cle, valeur in surcharges.items() if valeur})

    rendu = modele.rendre(court=channel in CANAUX_COURTS, **valeurs)

    suggestion = {}
    if avec_modele:
        suggestion = drafting.personnaliser(
            application, rendu, channel=channel, blind=blind
        )

    return Message.objects.create(
        application=application,
        channel=channel,
        direction=Message.Direction.OUTBOUND,
        status=Message.Status.DRAFT,
        subject=rendu["subject"],
        body=suggestion.get("body") or rendu["body"],
        template_id=rendu["template_id"],
        template_version=rendu["template_version"],
        prompt_id=suggestion.get("prompt_id", ""),
        prompt_version=suggestion.get("prompt_version", ""),
        model_name=suggestion.get("model", ""),
        drafted_by=actor if getattr(actor, "is_authenticated", False) else None,
        metadata={"blind": blind},
    )


# --- Envoi -------------------------------------------------------------------
def _marquer_echec(message: Message, exc: Exception) -> None:
    """Ecrit la cause de l'echec — **hors de toute transaction**.

    Enveloppee dans un `atomic`, cette ecriture serait annulee par l'exception
    qui la suit : le message resterait affiche « brouillon », sans motif, et le
    recruteur ne saurait pas pourquoi son envoi n'est pas parti. C'est
    exactement le contraire de ce que la trace sert a faire.
    """
    message.status = Message.Status.FAILED
    message.error = str(exc) if isinstance(exc, EnvoiRefuse) else (
        f"{type(exc).__name__} : {exc}"
    )
    message.save(update_fields=["status", "error", "updated_at"])


def envoyer(message: Message, *, actor=None, request=None) -> Message:
    """Fait partir un brouillon. Leve `EnvoiRefuse` si ce n'est pas possible."""
    if message.status == Message.Status.SENT:
        raise MessageDejaParti(
            "Ce message est deja parti. Un courrier lu ne se renvoie pas : "
            "rediger le suivant est la seule option honnete."
        )

    candidat = message.application.candidate
    if not autorise(candidat, message.channel):
        raise ConsentementManquant(
            f"{candidat.full_name} n'a pas autorise le contact par "
            f"{message.get_channel_display()}. Enregistrer son accord depuis "
            f"son dossier debloque l'envoi, sans reecrire le message."
        )

    try:
        detail = backends.expedier(message)
    except EnvoiRefuse as exc:
        _marquer_echec(message, exc)
        raise
    except Exception as exc:  # noqa: BLE001
        # Panne du transport : distincte d'un refus, et remontee comme telle.
        logger.exception("Envoi impossible pour %s", message.pk)
        _marquer_echec(message, exc)
        raise

    # Le courrier est parti : marquer l'envoi et le journaliser doivent reussir
    # ou echouer ensemble. Un message « envoye » sans entree d'audit rendrait
    # le journal incomplet la ou il sert le plus.
    with transaction.atomic():
        message.status = Message.Status.SENT
        message.sent_at = timezone.now()
        message.sent_by = actor if getattr(actor, "is_authenticated", False) else None
        message.error = ""
        message.metadata = {**message.metadata, **detail}
        message.save(
            update_fields=[
                "status", "sent_at", "sent_by", "error", "metadata", "updated_at",
            ]
        )

        record_audit(
            AuditLog.Action.MESSAGE_SENT,
            actor=actor,
            obj=message.application,
            summary=(
                f"{message.get_channel_display()} envoye "
                f"({message.template_id or 'texte libre'})"
            ),
            request=request,
            message=str(message.pk),
            channel=message.channel,
            template=message.template_id,
            template_version=message.template_version,
            # Un courrier redige par un modele doit rester identifiable comme
            # tel six mois plus tard, prompt et version compris.
            redige_par_un_modele=message.redige_par_un_modele,
            prompt=message.prompt_id,
            prompt_version=message.prompt_version,
        )
    return message


def consigner(
    application: Application,
    *,
    channel: str,
    body: str,
    actor=None,
    direction: str = Message.Direction.INBOUND,
    subject: str = "",
    request=None,
    **metadata,
) -> Message:
    """Note un echange qui n'est pas passe par le systeme : appel, reponse recue.

    Sans cela le journal ne dirait que ce que le logiciel a expedie, alors que
    l'essentiel d'un recrutement se dit au telephone. Le statut est `Consigne`
    et non `Envoye` : c'est une declaration humaine, pas une trace technique,
    et confondre les deux donnerait au journal une autorite qu'il n'a pas.
    """
    message = Message.objects.create(
        application=application,
        channel=channel,
        direction=direction,
        status=Message.Status.LOGGED,
        subject=subject,
        body=body.strip(),
        sent_by=actor if getattr(actor, "is_authenticated", False) else None,
        sent_at=timezone.now(),
        metadata=metadata,
    )
    record_audit(
        AuditLog.Action.MESSAGE_LOGGED,
        actor=actor,
        obj=application,
        summary=f"{message.get_channel_display()} consigne ({message.get_direction_display()})",
        request=request,
        message=str(message.pk),
        channel=channel,
        direction=direction,
    )
    return message


def echanges(application: Application):
    """Fil complet d'une candidature, du plus recent au plus ancien."""
    return (
        Message.objects.filter(application=application)
        .select_related("drafted_by", "sent_by")
        .order_by("-created_at")
    )
