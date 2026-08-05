"""Le silence : ce qu'on n'a pas dit aux candidats.

La plainte la plus repandue sur le recrutement n'est pas le refus, c'est
l'absence de reponse. Un systeme qui outille l'envoi de messages sans mesurer
ceux qu'on n'envoie pas outille surtout le confort du recruteur.

Deux silences distincts, et les confondre serait perdre le plus grave.

**Le silence apres une decision.** Un dossier ecarte, une decision datee, un
motif ecrit — et personne n'a prevenu l'interesse. C'est le pire des deux :
l'information existe, elle est ecrite, et elle n'est pas transmise.

**Le silence avant toute decision.** Un dossier ouvert depuis des semaines sans
un seul message. Le candidat ne sait meme pas si sa candidature est arrivee.

Le module ne calcule rien qui demande un modele de langage, et il ne bloque
rien. Il compte, il date, et il nomme les dossiers concernes — un taux sans la
liste ne se traite pas.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from django.utils import timezone

from apps.candidates.models import Application

from .models import Message

# Au-dela, un dossier ouvert sans un seul message devient un silence, pas un
# delai. Choisi sur le delai que les gabarits annoncent eux-memes : promettre
# une reponse sous quinze jours et se taire vingt-et-un jours est un manquement
# a sa propre promesse, pas un alea.
JOURS_AVANT_SILENCE = 21

# Etapes qui ferment un dossier et appellent donc une reponse.
ETAPES_FERMEES = (Application.Stage.REJECTED, Application.Stage.WITHDRAWN)


@dataclass(frozen=True)
class Oubli:
    """Un dossier reste sans reponse, et depuis combien de temps."""

    application: Application
    jours: int
    apres_decision: bool

    @property
    def candidat(self) -> str:
        return self.application.candidate.full_name


@dataclass
class Silence:
    """Ce que le processus n'a pas dit."""

    ecartes: int = 0
    ecartes_prevenus: int = 0
    ouverts_anciens: int = 0
    ouverts_sans_message: int = 0
    delai_median_jours: float | None = None
    oublis: list[Oubli] = field(default_factory=list)

    @property
    def ecartes_sans_reponse(self) -> int:
        return self.ecartes - self.ecartes_prevenus

    @property
    def taux_de_silence(self) -> float:
        return self.ecartes_sans_reponse / self.ecartes if self.ecartes else 0.0

    @property
    def pourcentage(self) -> int:
        return round(self.taux_de_silence * 100)

    @property
    def plus_ancien(self) -> Oubli | None:
        return self.oublis[0] if self.oublis else None

    @property
    def irreprochable(self) -> bool:
        return not self.oublis

    @property
    def lecture(self) -> str:
        """Ce que les chiffres disent, sans les arrondir dans le bon sens."""
        if not self.ecartes and not self.ouverts_anciens:
            return (
                "Aucun dossier ecarte et aucun dossier ouvert depuis plus de "
                f"{JOURS_AVANT_SILENCE} jours : il n'y a rien a reprocher, et "
                "rien a mesurer non plus."
            )
        if self.irreprochable:
            return (
                "Tous les candidats ecartes ont ete prevenus, et aucun dossier "
                "ouvert ne traine sans un message. C'est l'etat normal, pas un "
                "exploit — c'est simplement rare."
            )

        morceaux = []
        if self.ecartes_sans_reponse:
            morceaux.append(
                f"{self.ecartes_sans_reponse} candidat(s) sur {self.ecartes} ont "
                f"ete ecartes sans jamais etre prevenus ({self.pourcentage} %). "
                f"Le motif est ecrit dans leur dossier ; il n'est pas parti."
            )
        if self.ouverts_sans_message:
            morceaux.append(
                f"{self.ouverts_sans_message} dossier(s) ouverts depuis plus de "
                f"{JOURS_AVANT_SILENCE} jours n'ont recu aucun message : ces "
                f"candidats ignorent meme que leur candidature est arrivee."
            )
        return " ".join(morceaux)


def _messages_sortants_par_candidature() -> dict:
    """Date du premier message sortant, par candidature.

    Un appel telephonique consigne compte autant qu'un e-mail expedie : la
    question est « cette personne a-t-elle eu une reponse », pas « le logiciel
    a-t-il expedie quelque chose ».
    """
    dates: dict = {}
    lignes = (
        Message.objects.filter(
            direction=Message.Direction.OUTBOUND,
            status__in=(Message.Status.SENT, Message.Status.LOGGED),
        )
        .values_list("application_id", "sent_at", "created_at")
        .order_by("created_at")
    )
    for application_id, envoye_le, cree_le in lignes:
        moment = envoye_le or cree_le
        actuel = dates.get(application_id)
        if actuel is None or moment < actuel:
            dates[application_id] = moment
    return dates


def mesurer(*, offer=None) -> Silence:
    """Compte les dossiers laisses sans reponse."""
    candidatures = Application.objects.select_related("candidate", "offer")
    if offer is not None:
        candidatures = candidatures.filter(offer=offer)

    premiers_messages = _messages_sortants_par_candidature()
    maintenant = timezone.now()
    mesure = Silence()
    delais: list[float] = []
    oublis: list[Oubli] = []

    for candidature in candidatures:
        premier = premiers_messages.get(candidature.pk)

        if candidature.stage in ETAPES_FERMEES and candidature.decided_at:
            mesure.ecartes += 1
            # Un message envoye AVANT la decision ne previent de rien : il faut
            # une reponse posterieure au moment ou le sort du dossier est fixe.
            prevenu = premier is not None and premier >= candidature.decided_at
            if prevenu:
                mesure.ecartes_prevenus += 1
                delais.append((premier - candidature.decided_at).total_seconds() / 86400)
            else:
                oublis.append(
                    Oubli(
                        application=candidature,
                        jours=(maintenant - candidature.decided_at).days,
                        apres_decision=True,
                    )
                )
            continue

        if candidature.is_closed:
            continue

        anciennete = (maintenant - candidature.applied_at).days
        if anciennete < JOURS_AVANT_SILENCE:
            continue
        mesure.ouverts_anciens += 1
        if premier is None:
            mesure.ouverts_sans_message += 1
            oublis.append(
                Oubli(application=candidature, jours=anciennete, apres_decision=False)
            )

    # Le plus ancien d'abord : c'est celui dont l'absence de reponse coute le
    # plus cher, et celui par lequel il faut commencer.
    mesure.oublis = sorted(oublis, key=lambda item: -item.jours)
    mesure.delai_median_jours = (
        round(statistics.median(delais), 1) if delais else None
    )
    return mesure
