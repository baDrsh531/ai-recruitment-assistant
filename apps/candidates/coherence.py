"""Controles de coherence sur un dossier de candidature.

Ce module cherche des **incoherences verifiables**, pas des intentions. Chaque
signalement est une regle arithmetique ou calendaire qu'on peut rejouer a la
main : deux emplois a temps plein qui se chevauchent de dix-huit mois, un
diplome obtenu apres l'experience qu'il est cense avoir permise, une anciennete
declaree superieure a la somme des periodes citees.

**Ce que ce module ne fait pas, et pourquoi.**

Il ne cherche pas a deviner si un CV a ete redige par un modele de langage. Les
detecteurs de texte genere affichent des taux de faux positifs de l'ordre de 10
a 30 %, et surtout ils sur-signalent les locuteurs non natifs : un anglais
scolaire correct ressemble statistiquement a du texte genere. Dans un outil de
recrutement, cela produit une discrimination mesurable — exactement celle que
l'audit de biais de ce projet passe son temps a traquer. Un signalement qu'on ne
peut ni verifier ni expliquer a un candidat n'a pas sa place ici.

Il ne dit pas non plus « ce candidat ment ». Un chevauchement d'emplois peut
etre un cumul declare, une mission de conseil, un conge sabbatique mal date, ou
une erreur de saisie de l'extraction. Le module produit **des questions a poser
en entretien**, et le libelle de chaque signalement est ecrit pour cela.

Aucun signalement ne modifie le score. Le classement reste le fait du moteur
deterministe ; ceci est une lecture separee du meme dossier.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .models import Application, Candidate

# Un chevauchement court est banal : un preavis, une passation, un mois de
# recouvrement entre deux contrats. En deca, on ne signale rien.
CHEVAUCHEMENT_TOLERE_JOURS = 62

# Ecart entre l'anciennete declaree et la somme des periodes citees, au-dela
# duquel l'ecart merite une question. Un CV ne liste jamais tout.
ECART_ANCIENNETE_TOLERE = 2.0

# Un trou de moins de six mois entre deux postes n'a rien de remarquable.
TROU_SIGNIFICATIF_JOURS = 183

# Age plancher a l'obtention d'un diplome : en deca, c'est une date erronee.
AGE_MINIMAL_DIPLOME = 1980


@dataclass
class Signalement:
    """Une incoherence constatee, et la question qu'elle appelle."""

    code: str
    gravite: str  # information | attention
    titre: str
    detail: str
    question: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "gravite": self.gravite,
            "titre": self.titre,
            "detail": self.detail,
            "question": self.question,
        }


@dataclass
class Rapport:
    candidate: Candidate
    signalements: list[Signalement] = field(default_factory=list)
    periodes_datees: int = 0
    periodes_totales: int = 0

    @property
    def count(self) -> int:
        return len(self.signalements)

    @property
    def attention(self) -> list[Signalement]:
        return [item for item in self.signalements if item.gravite == "attention"]

    @property
    def verifiable(self) -> bool:
        """Y a-t-il assez de dates pour que l'absence de signalement veuille dire
        quelque chose ?

        Un CV sans dates ne declenche aucun controle. Afficher « aucune
        incoherence » serait alors trompeur : rien n'a pu etre verifie.
        """
        return self.periodes_datees >= 2

    @property
    def coverage(self) -> float:
        if not self.periodes_totales:
            return 0.0
        return self.periodes_datees / self.periodes_totales

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "verifiable": self.verifiable,
            "dated_periods": self.periodes_datees,
            "total_periods": self.periodes_totales,
            "coverage": round(self.coverage, 3),
            "findings": [item.as_dict() for item in self.signalements],
        }


def _mois(jours: int) -> str:
    valeur = jours / 30.44
    if valeur >= 12:
        return f"{valeur / 12:.1f} an(s)"
    return f"{valeur:.0f} mois"


def _fin(experience, aujourdhui: dt.date) -> dt.date:
    return experience.end_date or aujourdhui


# --- Controles ---------------------------------------------------------------
def _chevauchements(experiences, aujourdhui) -> list[Signalement]:
    """Deux periodes qui se recouvrent au-dela de la tolerance."""
    datees = sorted(
        (item for item in experiences if item.start_date),
        key=lambda item: item.start_date,
    )
    signalements: list[Signalement] = []

    for index, premiere in enumerate(datees):
        for seconde in datees[index + 1:]:
            debut = max(premiere.start_date, seconde.start_date)
            fin = min(_fin(premiere, aujourdhui), _fin(seconde, aujourdhui))
            recouvrement = (fin - debut).days
            if recouvrement <= CHEVAUCHEMENT_TOLERE_JOURS:
                continue
            signalements.append(
                Signalement(
                    code="chevauchement",
                    gravite="attention",
                    titre=f"Deux postes se recouvrent sur {_mois(recouvrement)}",
                    detail=(
                        f"« {premiere.title} » chez {premiere.company or 'employeur non cite'} "
                        f"et « {seconde.title} » chez {seconde.company or 'employeur non cite'} "
                        f"se chevauchent du {debut:%d/%m/%Y} au {fin:%d/%m/%Y}."
                    ),
                    question=(
                        "Ces deux postes ont-ils ete occupes en parallele — cumul, "
                        "mission de conseil, temps partiel ? Sinon, l'une des deux "
                        "dates est a corriger."
                    ),
                )
            )
    return signalements


def _diplome_apres_experience(candidat, experiences) -> list[Signalement]:
    """Un diplome obtenu apres l'experience qu'il est cense avoir permise."""
    datees = [item for item in experiences if item.start_date]
    if not datees:
        return []

    premiere = min(item.start_date for item in datees)
    signalements: list[Signalement] = []

    for formation in candidat.education.all():
        annee = formation.graduation_year
        if not annee or annee < AGE_MINIMAL_DIPLOME:
            continue
        # Un diplome obtenu en cours d'emploi est courant (formation continue) :
        # on ne signale que l'ecart franc, superieur a deux ans.
        if annee - premiere.year >= 2:
            signalements.append(
                Signalement(
                    code="diplome_posterieur",
                    gravite="information",
                    titre=f"Diplome obtenu {annee - premiere.year} ans apres le premier poste",
                    detail=(
                        f"« {formation.degree} » est date de {annee}, alors que la "
                        f"premiere experience citee commence en {premiere.year}."
                    ),
                    question=(
                        "S'agit-il d'une formation continue, d'une reprise d'etudes "
                        "ou d'une alternance ? Le parcours se lit differemment selon "
                        "la reponse."
                    ),
                )
            )
    return signalements


def _anciennete_declaree(candidat, experiences, aujourdhui) -> list[Signalement]:
    """Anciennete declaree tres superieure a la somme des periodes citees."""
    datees = [item for item in experiences if item.start_date]
    if not datees:
        return []

    # Union des periodes : deux missions menees en parallele ne comptent
    # qu'une fois, comme dans le calcul d'anciennete du moteur.
    intervalles = sorted(
        (item.start_date, _fin(item, aujourdhui)) for item in datees
    )
    fusionnes: list[list[dt.date]] = []
    for debut, fin in intervalles:
        if fusionnes and debut <= fusionnes[-1][1]:
            fusionnes[-1][1] = max(fusionnes[-1][1], fin)
        else:
            fusionnes.append([debut, fin])

    couverte = sum((fin - debut).days for debut, fin in fusionnes) / 365.25
    ecart = candidat.total_experience_years - couverte
    if ecart <= ECART_ANCIENNETE_TOLERE:
        return []

    return [
        Signalement(
            code="anciennete_non_couverte",
            gravite="information",
            titre=f"{ecart:.1f} an(s) d'anciennete sans periode correspondante",
            detail=(
                f"Le profil declare {candidat.total_experience_years:.1f} an(s) "
                f"d'experience ; les periodes datees du CV en couvrent "
                f"{couverte:.1f}."
            ),
            question=(
                "Quelles experiences ne figurent pas sur le CV ? L'ecart peut "
                "aussi venir de dates non extraites du document."
            ),
        )
    ]


def _trous(experiences, aujourdhui) -> list[Signalement]:
    """Interruptions longues entre deux postes.

    Signalees en information et jamais en attention : une interruption est
    souvent une maternite, une maladie, un proche a charge ou une reconversion.
    En faire un signal negatif serait discriminatoire ; la mentionner permet
    seulement d'en parler si le candidat le souhaite.
    """
    datees = sorted(
        (item for item in experiences if item.start_date),
        key=lambda item: item.start_date,
    )
    if len(datees) < 2:
        return []

    signalements: list[Signalement] = []
    fin_courante = _fin(datees[0], aujourdhui)
    for experience in datees[1:]:
        trou = (experience.start_date - fin_courante).days
        if trou > TROU_SIGNIFICATIF_JOURS:
            signalements.append(
                Signalement(
                    code="interruption",
                    gravite="information",
                    titre=f"Interruption de {_mois(trou)}",
                    detail=(
                        f"Entre {fin_courante:%m/%Y} et "
                        f"{experience.start_date:%m/%Y}, aucune periode n'est citee."
                    ),
                    question=(
                        "A n'aborder que si le candidat le souhaite. Une "
                        "interruption n'est pas un signal negatif et ne doit "
                        "jamais peser sur la decision."
                    ),
                )
            )
        fin_courante = max(fin_courante, _fin(experience, aujourdhui))
    return signalements


def _dates_impossibles(experiences) -> list[Signalement]:
    """Fin anterieure au debut, ou date dans le futur."""
    aujourdhui = dt.date.today()
    signalements: list[Signalement] = []
    for experience in experiences:
        if (
            experience.start_date
            and experience.end_date
            and experience.end_date < experience.start_date
        ):
            signalements.append(
                Signalement(
                    code="dates_inversees",
                    gravite="attention",
                    titre="Date de fin anterieure a la date de debut",
                    detail=(
                        f"« {experience.title} » : du "
                        f"{experience.start_date:%d/%m/%Y} au "
                        f"{experience.end_date:%d/%m/%Y}."
                    ),
                    question=(
                        "Erreur de saisie ou d'extraction : la periode est a "
                        "confirmer avant toute lecture du parcours."
                    ),
                )
            )
        if experience.start_date and experience.start_date > aujourdhui:
            signalements.append(
                Signalement(
                    code="date_future",
                    gravite="attention",
                    titre="Poste commencant dans le futur",
                    detail=(
                        f"« {experience.title} » debute le "
                        f"{experience.start_date:%d/%m/%Y}."
                    ),
                    question=(
                        "S'agit-il d'une prise de poste a venir, ou d'une date mal "
                        "lue par l'extraction ?"
                    ),
                )
            )
    return signalements


# --- Point d'entree ----------------------------------------------------------
def analyse(candidat: Candidate, *, today: dt.date | None = None) -> Rapport:
    """Passe le dossier au crible des controles de coherence."""
    aujourdhui = today or dt.date.today()
    experiences = list(candidat.experiences.all())

    rapport = Rapport(
        candidate=candidat,
        periodes_totales=len(experiences),
        periodes_datees=sum(1 for item in experiences if item.start_date),
    )
    rapport.signalements = [
        *_dates_impossibles(experiences),
        *_chevauchements(experiences, aujourdhui),
        *_diplome_apres_experience(candidat, experiences),
        *_anciennete_declaree(candidat, experiences, aujourdhui),
        *_trous(experiences, aujourdhui),
    ]
    # Les points d'attention d'abord : ce sont les seuls qui appellent une
    # verification avant de lire le reste du dossier.
    rapport.signalements.sort(key=lambda item: item.gravite != "attention")
    return rapport


def for_application(application: Application, **kwargs) -> Rapport:
    return analyse(application.candidate, **kwargs)
