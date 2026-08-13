"""Rejouer les decisions passees avec le moteur d'aujourd'hui.

Le projet affirme partout que le score est **deterministe et reproductible**.
Ce module cesse de l'affirmer et le verifie, sur les vraies decisions du vrai
historique : on reprend le dossier tel qu'il etait juge, on recalcule, et on
regarde si le chiffre a bouge.

**La difficulte n'est pas de recalculer, elle est d'attribuer l'ecart.** Un
score qui change six mois plus tard peut venir de deux causes qui n'ont rien a
voir :

- le **moteur** a change de version — c'est ce qu'on cherche a mesurer ;
- les **donnees** ont change — le CV a ete re-extrait, une competence corrigee,
  la ponderation de l'offre revue. Le moteur est alors innocent, et le rejeu ne
  prouve rien.

Confondre les deux donnerait un chiffre flatteur ou alarmant selon le sens du
vent. Chaque rejeu porte donc la mention `concluant`, et le rapport ne compte
comme divergence que ce qui l'est.

Ce qui interesse un auditeur n'est d'ailleurs pas l'ecart de score mais son
effet : **la decision aurait-elle bascule ?** Un dossier qui passe de 0.91 a
0.90 n'a rien change ; un dossier qui passe de 0.86 a 0.84 sous un seuil a 0.85
en a change beaucoup.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from apps.candidates.models import Application
from apps.matching.engine import ENGINE_VERSION
from apps.matching.engine import score as calculer
from apps.matching.models import MatchScore

# Ecart en deca duquel deux scores sont tenus pour identiques. Le moteur est
# deterministe, mais il additionne des flottants : deux executions peuvent
# differer sur le dernier bit sans que rien n'ait change. Un demi-point de
# pourcentage est largement au-dessus de ce bruit et tres en dessous de ce qui
# ferait basculer une decision.
TOLERANCE = 0.0005

ETAPES_DECIDEES = (
    Application.Stage.REJECTED,
    Application.Stage.WITHDRAWN,
    Application.Stage.HIRED,
)


@dataclass(frozen=True)
class Rejeu:
    """Une decision passee, recalculee aujourd'hui."""

    application: Application
    score_alors: float
    version_alors: str
    score_maintenant: float
    version_maintenant: str
    seuil: float
    # Les donnees du dossier ont-elles bouge depuis la decision ? Si oui, le
    # rejeu ne dit rien du moteur.
    donnees_modifiees: bool
    blind: bool
    # Un recruteur avait-il corrige le score a la main ? La decision ne reposait
    # alors pas sur le chiffre du moteur, et le rejeu ne l'atteint pas.
    corrige_a_la_main: bool = False

    @property
    def ecart(self) -> float:
        return self.score_maintenant - self.score_alors

    @property
    def points(self) -> float:
        """Ecart en points de pourcentage, arrondi pour l'affichage."""
        return round(self.ecart * 100, 1)

    @property
    def identique(self) -> bool:
        return abs(self.ecart) < TOLERANCE

    @property
    def concluant(self) -> bool:
        """Le rejeu permet-il de conclure quoi que ce soit sur le moteur ?"""
        return not self.donnees_modifiees

    @property
    def meme_moteur(self) -> bool:
        return self.version_alors == self.version_maintenant

    @property
    def bascule(self) -> bool:
        """Le dossier aurait-il change de cote du seuil ?

        C'est la seule divergence qui coute quelque chose a quelqu'un. Sur un
        dossier corrige a la main, la question ne se pose pas : le seuil ne
        s'appliquait pas au chiffre du moteur.
        """
        if self.corrige_a_la_main:
            return False
        return (self.score_alors >= self.seuil) != (self.score_maintenant >= self.seuil)

    @property
    def gravite(self) -> str:
        if not self.concluant:
            return "non concluant"
        if self.bascule:
            return "bascule"
        if not self.identique:
            return "ecart"
        return "identique"


@dataclass
class Rapport:
    """Ce que le rejeu de l'historique apprend."""

    rejeux: list[Rejeu] = field(default_factory=list)
    version_courante: str = ENGINE_VERSION
    sans_score: int = 0

    @property
    def total(self) -> int:
        return len(self.rejeux)

    @property
    def concluants(self) -> list[Rejeu]:
        return [item for item in self.rejeux if item.concluant]

    @property
    def non_concluants(self) -> list[Rejeu]:
        return [item for item in self.rejeux if not item.concluant]

    @property
    def identiques(self) -> list[Rejeu]:
        return [item for item in self.concluants if item.identique]

    @property
    def divergents(self) -> list[Rejeu]:
        return [item for item in self.concluants if not item.identique]

    @property
    def bascules(self) -> list[Rejeu]:
        return [item for item in self.concluants if item.bascule]

    @property
    def reproductible(self) -> bool:
        """Aucun ecart sur un dossier juge par la meme version du moteur.

        C'est l'affirmation exacte que le projet fait. Une divergence entre
        deux versions differentes est attendue et documentee ; une divergence a
        version egale serait un defaut.
        """
        return not [
            item for item in self.divergents if item.meme_moteur
        ]

    @property
    def ecart_median(self) -> float | None:
        ecarts = [abs(item.points) for item in self.divergents]
        return round(statistics.median(ecarts), 2) if ecarts else None

    @property
    def par_transition(self) -> list[dict]:
        """Divergences regroupees par passage de version, la plus peuplee devant.

        Un auditeur ne demande pas « combien de scores ont bouge » mais « qu'est-ce
        qui les a fait bouger ». La reponse est presque toujours un changement
        de version precis.
        """
        groupes: dict[tuple[str, str], list[Rejeu]] = {}
        for item in self.divergents:
            groupes.setdefault((item.version_alors, item.version_maintenant), []).append(item)
        return sorted(
            (
                {
                    "de": de,
                    "vers": vers,
                    "nombre": len(lot),
                    "bascules": sum(1 for item in lot if item.bascule),
                    "ecart_max": max(abs(item.points) for item in lot),
                }
                for (de, vers), lot in groupes.items()
            ),
            key=lambda ligne: -ligne["nombre"],
        )

    @property
    def lecture(self) -> str:
        """Ce que les chiffres autorisent a dire."""
        if not self.total:
            return (
                "Aucune decision a rejouer. Le moteur ne peut etre eprouve que "
                "sur des dossiers reellement tranches."
            )
        if not self.concluants:
            return (
                f"{self.total} decision(s) rejouee(s), aucune exploitable : les "
                f"donnees de chaque dossier ont change depuis. Le moteur n'y est "
                f"peut-etre pour rien, et rien ne permet de l'affirmer."
            )

        base = (
            f"{len(self.concluants)} decision(s) rejouable(s) sur {self.total}, "
            f"{len(self.identiques)} au chiffre pres."
        )
        if not self.divergents:
            return (
                f"{base} Aucun ecart : le moteur rend aujourd'hui exactement ce "
                f"qu'il rendait alors."
            )
        if self.reproductible:
            return (
                f"{base} {len(self.divergents)} ecart(s), tous imputables a un "
                f"changement de version du moteur — aucun a version egale, ce "
                f"qui est l'affirmation que le projet fait. "
                f"{len(self.bascules)} auraient change de cote du seuil."
            )
        return (
            f"{base} {len(self.divergents)} ecart(s), dont certains **a version "
            f"de moteur egale**. Le score n'est donc pas reproductible : c'est "
            f"un defaut, pas une evolution."
        )


def _score_au_moment(application: Application, quand) -> MatchScore | None:
    """Score en vigueur au moment de la decision.

    Le dernier calcule **avant** la decision, pas le plus recent : rejouer
    contre un score posterieur comparerait le moteur a lui-meme.
    """
    return (
        application.scores.filter(created_at__lte=quand)
        .order_by("-created_at")
        .first()
    )


def _donnees_modifiees(application: Application, depuis) -> bool:
    """Le dossier a-t-il bouge depuis la decision ?

    On regarde le candidat, l'offre et leurs elements portant une date. Une
    modification posterieure suffit a rendre le rejeu muet : impossible de dire
    si l'ecart vient du moteur ou de la donnee.
    """
    candidat, offre = application.candidate, application.offer
    dates = [candidat.updated_at, offre.updated_at]
    dates += [item.updated_at for item in candidat.skills.all()]
    dates += [item.updated_at for item in candidat.experiences.all()]
    dates += [item.updated_at for item in candidat.languages.all()]
    dates += [item.updated_at for item in offre.skills.all()]
    return any(date > depuis for date in dates if date is not None)


def rejouer(*, offer=None, limit: int | None = None) -> Rapport:
    """Recalcule les decisions tranchees et compare au score d'alors."""
    from apps.evaluation import threshold as calibration

    candidatures = (
        Application.objects.filter(
            stage__in=ETAPES_DECIDEES, decided_at__isnull=False
        )
        .select_related("candidate", "offer")
        .prefetch_related(
            "scores",
            "candidate__skills",
            "candidate__experiences",
            "candidate__languages",
            "offer__skills",
        )
        .order_by("-decided_at")
    )
    if offer is not None:
        candidatures = candidatures.filter(offer=offer)
    if limit:
        candidatures = candidatures[:limit]

    seuil = calibration.recommended_threshold()
    rapport = Rapport()

    for candidature in candidatures:
        alors = _score_au_moment(candidature, candidature.decided_at)
        if alors is None:
            # Dossier tranche sans qu'un score ait ete calcule avant : il n'y a
            # rien a comparer. Compte a part plutot qu'ignore en silence.
            rapport.sans_score += 1
            continue

        recalcule = calculer(
            candidature.candidate, candidature.offer, blind=alors.blind
        )
        rapport.rejeux.append(
            Rejeu(
                application=candidature,
                # `overall` et non `effective_score` : on confronte le moteur au
                # moteur. Prendre le score corrige a la main comparerait un
                # chiffre humain a un chiffre calcule, et tout dossier corrige
                # apparaitrait comme une divergence du moteur.
                score_alors=alors.overall,
                version_alors=alors.engine_version,
                score_maintenant=recalcule.overall,
                version_maintenant=recalcule.engine_version,
                seuil=seuil,
                donnees_modifiees=_donnees_modifiees(
                    candidature, candidature.decided_at
                ),
                blind=alors.blind,
                corrige_a_la_main=alors.is_overridden,
            )
        )
    return rapport
