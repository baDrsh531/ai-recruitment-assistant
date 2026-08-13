"""Le score sature-t-il, et a partir de combien de candidats ?

Le moteur ramene chaque criterion dans [0, 1] puis applique un facteur de
recevabilite. Un profil qui satisfait toutes les exigences atteint donc 1.0, et
**plusieurs profils differents peuvent y arriver ensemble**. Au plafond, le
moteur ne distingue plus rien : l'ordre entre eux ne vient plus du score mais de
ce qui departage les egalites, c'est-a-dire du hasard de l'insertion.

Le defaut est apparu en portant le jeu annote a trente cas : sur un vivier de
dix profils, un expert de dix ans et un confirme de cinq ans atteignaient tous
deux 1.000.

**Il n'a pas fallu dix candidats pour cela.** La mesure montre des confusions
des quatre profils dans un meme vivier, et 28,8 % des candidats du jeu au
plafond. Le defaut etait donc deja present sur les cas d'origine a cinq
candidats — il n'y etait simplement pas cherche. C'est la raison d'etre de ce
module : ce qu'on ne compte pas, on ne le voit pas.

**Ce module ne corrige rien.** Corriger demanderait de changer la ponderation ou
d'etaler le haut de l'echelle, ce qui est un arbitrage produit : un recruteur
peut tres bien vouloir qu'un profil « parfait » soit parfait. Le module mesure
l'ampleur du probleme pour que cet arbitrage se prenne sur des chiffres.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# Un score au-dela de ce seuil est traite comme « au plafond ». Pas 1.0 exact :
# le moteur additionne des flottants, et deux profils egaux peuvent differer sur
# le dernier bit.
PLAFOND = 0.9995

# En deca, une egalite ne coute rien : deux candidats a departager se lisent
# cote a cote. Au-dela, le recruteur voit un bloc indifferencie en tete de
# classement.
EGALITE_GENANTE = 3


@dataclass(frozen=True)
class CasSature:
    """Un cas ou plusieurs profils atteignent le plafond."""

    cas: str
    candidats: int
    au_plafond: int
    identifiants: list[str]
    pertinences: list[int]

    @property
    def part(self) -> float:
        return self.au_plafond / self.candidats if self.candidats else 0.0

    @property
    def pertinences_confondues(self) -> bool:
        """Le plafond ecrase-t-il des profils que l'annotation separe ?

        C'est la seule forme de saturation qui coute quelque chose. Deux
        profils egaux pour un recruteur ET egaux pour le moteur ne posent
        aucun probleme ; deux profils que le recruteur separe et que le moteur
        confond en posent un.
        """
        return len(set(self.pertinences)) > 1

    @property
    def genant(self) -> bool:
        return self.pertinences_confondues or self.au_plafond >= EGALITE_GENANTE


@dataclass
class Mesure:
    """Ce que la saturation coute sur le jeu annote."""

    cas_total: int = 0
    candidats_total: int = 0
    au_plafond: int = 0
    satures: list[CasSature] = field(default_factory=list)
    # Nombre de candidats au plafond, par cas : sert a voir a partir de quel
    # effectif la saturation apparait.
    par_effectif: Counter = field(default_factory=Counter)

    @property
    def part_au_plafond(self) -> float:
        return self.au_plafond / self.candidats_total if self.candidats_total else 0.0

    @property
    def genants(self) -> list[CasSature]:
        return [item for item in self.satures if item.genant]

    @property
    def confusions(self) -> list[CasSature]:
        """Cas ou le plafond confond des profils que l'annotation separe."""
        return [item for item in self.satures if item.pertinences_confondues]

    @property
    def effectif_declencheur(self) -> int | None:
        """Plus petit vivier ou une confusion apparait.

        Repond a « a partir de combien de candidats est-ce que ca devient un
        probleme ? », qui est la question qu'un recruteur pose.
        """
        tailles = [item.candidats for item in self.confusions]
        return min(tailles) if tailles else None

    @property
    def lecture(self) -> str:
        if not self.satures:
            return (
                f"Aucun profil au plafond sur {self.candidats_total} candidats. "
                f"Le score departage partout ou il est sollicite."
            )
        base = (
            f"{self.au_plafond} candidat(s) sur {self.candidats_total} "
            f"atteignent le plafond, repartis sur {len(self.satures)} cas."
        )
        if not self.confusions:
            return (
                f"{base} Aucune confusion : partout ou plusieurs profils sont "
                f"a 1,000, l'annotation les tient pour equivalents. La "
                f"saturation n'a donc rien coute ici."
            )
        return (
            f"{base} **{len(self.confusions)} cas confondent des profils que "
            f"l'annotation separe** — c'est la seule forme de saturation qui "
            f"coute quelque chose. Elle apparait des "
            f"{self.effectif_declencheur} candidats."
        )


def mesurer(dataset_name: str = "ranking_v1") -> Mesure:
    """Rejoue le jeu annote et compte les egalites au plafond."""
    from . import harness

    rapport = harness.run(dataset_name)
    mesure = Mesure(cas_total=len(rapport.cases))

    for cas in rapport.cases:
        mesure.candidats_total += len(cas.scores)
        plafonnes = [
            (identifiant, pertinence)
            for identifiant, pertinence, score in zip(
                cas.predicted_order, cas.relevances, cas.scores, strict=True
            )
            if score >= PLAFOND
        ]
        mesure.au_plafond += len(plafonnes)

        # Un seul profil au plafond n'est pas une saturation : c'est le
        # meilleur, et rien ne le confond avec personne.
        if len(plafonnes) < 2:
            continue

        mesure.par_effectif[len(cas.scores)] += 1
        mesure.satures.append(
            CasSature(
                cas=cas.id,
                candidats=len(cas.scores),
                au_plafond=len(plafonnes),
                identifiants=[identifiant for identifiant, _ in plafonnes],
                pertinences=[pertinence for _, pertinence in plafonnes],
            )
        )
    return mesure
