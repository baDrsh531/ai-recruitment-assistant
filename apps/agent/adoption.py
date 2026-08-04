"""Ce que les recruteurs font vraiment des recommandations.

L'agent ne decide pas : c'est une propriete du compte, verifiable dans le code.
Mais « il ne decide pas » et « la supervision est reelle » ne sont pas la meme
affirmation. Un recruteur qui suit 100 % des propositions sans jamais en
contredire une rend la garantie purement formelle — la decision lui est
imputee, mais elle est prise par la machine.

Ce module mesure la seule chose qui distingue les deux cas : le **taux de
contradiction**, la part des propositions qu'un humain a ecartees.

Il ne dit pas quel taux serait bon. Ni 0 % ni 100 % ne sont defendables — le
premier decrit un tampon, le second un agent inutile — mais entre les deux, la
valeur juste depend du metier, pas de ce module.

**L'incertitude est affichee avec le chiffre, pas en note de bas de page.** Un
taux de 25 % mesure sur quatre decisions est compatible avec a peu pres tout,
tampon compris. L'intervalle de Wilson le dit franchement : une contradiction
sur quatre decisions, c'est [5 %, 70 %]. Publier « 25 % » sans cet intervalle
serait le genre de chiffre qui rassure un comite sans rien prouver.

La ventilation par type de proposition n'est pas un ornement. « Les recruteurs
contredisent les propositions de rejet mais valident sans broncher les
propositions de mise en entretien » est un resultat different de « ils
contredisent 20 % du temps », et c'est le premier qui interesse un auditeur :
il decrit ou la supervision se relache.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from apps.candidates.models import Application

from .models import Recommendation

# En deca, le taux est affiche mais ne se lit pas. Le seuil est arbitraire et
# assume : ce qui ne l'est pas, c'est de refuser de conclure sur trois
# decisions.
MINIMUM_LISIBLE = 20

# Sous ce taux, l'agent est suivi presque toujours. Ce n'est pas une faute en
# soi — un agent juste merite d'etre suivi — mais c'est le point ou il faut
# aller regarder si les dossiers sont lus.
#
# Le signal se declenche sur la **borne haute** de l'intervalle, pas sur le
# taux : crier au tampon parce que trois decisions sur trois ont ete suivies
# serait du bruit. Sans aucune contradiction, cette borne vaut z²/(n+z²), donc
# 10 % est atteint a partir de 35 decisions — c'est le prix a payer pour que
# l'alerte, quand elle tombe, veuille dire quelque chose. Descendre le seuil a
# 5 % le porterait a 76 decisions, soit une alerte qui n'arriverait jamais.
SEUIL_TAMPON = 0.10


def _wilson(succes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance a 95 % d'une proportion.

    Wilson plutot que l'approximation normale : sur les petits effectifs — le
    cas courant ici — l'approximation normale sort des bornes negatives ou
    au-dessus de 1, ce qui se voit et decredibilise le reste de la page.
    """
    if total == 0:
        return (0.0, 1.0)
    p = succes / total
    denominateur = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominateur
    demi = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    ) / denominateur
    return (max(0.0, centre - demi), min(1.0, centre + demi))


@dataclass(frozen=True)
class Taux:
    """Une proportion, avec de quoi savoir si on peut la lire."""

    libelle: str
    contredites: int
    tranchees: int

    @property
    def valeur(self) -> float:
        return self.contredites / self.tranchees if self.tranchees else 0.0

    @property
    def pourcentage(self) -> int:
        return round(self.valeur * 100)

    @property
    def intervalle(self) -> tuple[float, float]:
        return _wilson(self.contredites, self.tranchees)

    @property
    def borne_basse(self) -> int:
        return round(self.intervalle[0] * 100)

    @property
    def borne_haute(self) -> int:
        return round(self.intervalle[1] * 100)

    @property
    def largeur(self) -> int:
        """Etendue de l'intervalle, en points. Sert au trace."""
        return self.borne_haute - self.borne_basse

    @property
    def lisible(self) -> bool:
        return self.tranchees >= MINIMUM_LISIBLE

    @property
    def suivies(self) -> int:
        return self.tranchees - self.contredites


@dataclass
class Adoption:
    """Ce que deviennent les propositions de l'agent."""

    global_: Taux
    par_type: list[Taux] = field(default_factory=list)
    en_attente: int = 0
    perimees: int = 0
    # Delai entre la proposition et la decision humaine. Preuve faible, gardee
    # pour ce qu'elle vaut : voir `lecture`.
    delai_median_min: float | None = None

    @property
    def total(self) -> int:
        return self.global_.tranchees + self.en_attente + self.perimees

    @property
    def assez_de_recul(self) -> bool:
        return self.global_.lisible

    @property
    def tampon_possible(self) -> bool:
        """Le taux est-il assez bas pour qu'il faille aller verifier ?

        Sur peu de decisions on ne conclut pas — mais la **borne haute** de
        l'intervalle, elle, se lit toujours : si meme le haut de la fourchette
        reste sous le seuil, l'agent n'est quasiment jamais contredit, et ca ne
        depend pas de l'effectif.
        """
        return self.global_.tranchees > 0 and self.global_.intervalle[1] < SEUIL_TAMPON

    @property
    def lecture(self) -> str:
        """Ce que le chiffre autorise a dire — et rien de plus."""
        if self.global_.tranchees == 0:
            return (
                "Aucune proposition n'a encore ete tranchee. Tant que ce nombre "
                "est nul, rien ne permet de dire si la supervision humaine est "
                "effective ou seulement prevue."
            )
        if self.tampon_possible:
            return (
                f"L'agent est contredit dans au plus {self.global_.borne_haute} % "
                f"des cas, borne haute comprise. Un agent qu'on ne contredit "
                f"jamais est soit parfait, soit valide sans etre lu — et la "
                f"seconde hypothese est la plus courante."
            )
        if not self.assez_de_recul:
            return (
                f"{self.global_.pourcentage} % de contradictions sur "
                f"{self.global_.tranchees} decision(s) : l'intervalle va de "
                f"{self.global_.borne_basse} % a {self.global_.borne_haute} %. "
                f"Trop large pour conclure. Il faut au moins "
                f"{MINIMUM_LISIBLE} decisions tranchees."
            )
        return (
            f"L'agent est contredit dans {self.global_.pourcentage} % des cas "
            f"(intervalle {self.global_.borne_basse}–{self.global_.borne_haute} %, "
            f"sur {self.global_.tranchees} decisions). Les recruteurs lisent les "
            f"dossiers et tranchent contre la proposition quand ils le jugent "
            f"utile."
        )

    @property
    def note_delai(self) -> str:
        """Pourquoi le delai median ne prouve pas grand-chose."""
        if self.delai_median_min is None:
            return ""
        return (
            "Ce delai mesure le temps entre la proposition et la decision. Il "
            "est domine par le moment ou le recruteur se connecte, pas par le "
            "temps qu'il passe a lire : un delai long ne prouve pas l'attention, "
            "un delai court ne prouve pas la negligence. Il est affiche parce "
            "qu'un delai median de quelques secondes, lui, serait un signal."
        )


LIBELLES = {
    Application.Stage.REJECTED: "Rejets proposes",
    Application.Stage.WITHDRAWN: "Retraits proposes",
    Application.Stage.SCREENING: "Mises en entretien proposees",
}

TRANCHEES = (Recommendation.Status.ACCEPTED, Recommendation.Status.REJECTED)


def mesurer(*, offer=None) -> Adoption:
    """Compte ce que les recruteurs ont fait des propositions.

    `offer` restreint a une offre : un taux global peut cacher un service qui
    valide tout et un autre qui relit tout.
    """
    recommandations = Recommendation.objects.all()
    if offer is not None:
        recommandations = recommandations.filter(application__offer=offer)

    tranchees = list(
        recommandations.filter(status__in=TRANCHEES).values_list(
            "proposed_stage", "status", "created_at", "resolved_at"
        )
    )

    contredites = sum(
        1 for _, statut, _, _ in tranchees if statut == Recommendation.Status.REJECTED
    )

    delais = [
        (resolue - creee).total_seconds() / 60
        for _, _, creee, resolue in tranchees
        if resolue is not None
    ]

    # La ventilation part des etapes reellement presentes, pas d'une liste
    # figee : une etape inconnue de `LIBELLES` disparaitrait de la ventilation
    # tout en comptant dans le total, et les deux ne s'additionneraient plus
    # sans que rien ne le signale.
    par_type: list[Taux] = []
    for etape in dict.fromkeys(item[0] for item in tranchees):
        lot = [item for item in tranchees if item[0] == etape]
        par_type.append(
            Taux(
                libelle=LIBELLES.get(etape, f"« {etape} » propose"),
                contredites=sum(
                    1 for _, statut, _, _ in lot
                    if statut == Recommendation.Status.REJECTED
                ),
                tranchees=len(lot),
            )
        )

    return Adoption(
        global_=Taux(
            libelle="Toutes propositions",
            contredites=contredites,
            tranchees=len(tranchees),
        ),
        par_type=sorted(par_type, key=lambda item: -item.tranchees),
        en_attente=recommandations.filter(
            status=Recommendation.Status.PENDING
        ).count(),
        perimees=recommandations.filter(
            status=Recommendation.Status.STALE
        ).count(),
        delai_median_min=round(statistics.median(delais), 1) if delais else None,
    )
