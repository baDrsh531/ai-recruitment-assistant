"""Simulateur de ponderation : « et si on comptait autrement ? »

La ponderation des criteres est le seul endroit ou un recruteur decide de ce
qui compte. Elle est modifiable offre par offre depuis l'origine — mais a
l'aveugle : on change un poids, on enregistre, et on decouvre le nouveau
classement apres coup.

Ce module rend l'arbitrage visible avant de l'appliquer. Il rejoue le moteur
avec une autre ponderation, montre le classement obtenu, **et** ce que la
nouvelle ponderation fait au ratio d'impact. Ce second point est le coeur du
sujet : donner plus de poids a la localisation ameliore peut-etre le
classement percu, et degrade mecaniquement le ratio d'impact du seul critere
que l'audit de biais a identifie comme porteur d'un signal identitaire. Un
simulateur qui montrerait le classement sans le ratio laisserait prendre cette
decision sans en voir le prix.

Rien n'est enregistre. La simulation passe par un parametre du moteur, jamais
par une ecriture temporaire sur l'offre : une simulation interrompue ne peut
pas laisser une offre avec des poids qui ne sont pas les siens.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.candidates.models import Application
from apps.jobs.models import DEFAULT_WEIGHTS, JobOffer

from . import engine

# Bornes d'un poids saisi. Un poids negatif inverserait le critere — « moins
# de competences vaut mieux » — et rien dans le moteur ne le prevoit.
POIDS_MIN = 0.0
POIDS_MAX = 3.0


@dataclass
class Ligne:
    """Une candidature, son rang avant et apres."""

    application: Application
    score_actuel: float
    score_simule: float
    rang_actuel: int
    rang_simule: int

    @property
    def delta_rang(self) -> int:
        """Positif = le candidat monte."""
        return self.rang_actuel - self.rang_simule

    @property
    def delta_points(self) -> float:
        return round((self.score_simule - self.score_actuel) * 100, 1)

    @property
    def percentage(self) -> int:
        return round(self.score_simule * 100)

    @property
    def a_bouge(self) -> bool:
        return self.rang_actuel != self.rang_simule


@dataclass
class Simulation:
    offer: JobOffer
    weights: dict[str, float]
    baseline_weights: dict[str, float]
    rows: list[Ligne] = field(default_factory=list)
    impact_ratio: float | None = None
    baseline_impact_ratio: float | None = None
    impact_dimension: str = ""

    @property
    def mouvements(self) -> int:
        return sum(1 for ligne in self.rows if ligne.a_bouge)

    @property
    def impact_delta(self) -> float | None:
        if self.impact_ratio is None or self.baseline_impact_ratio is None:
            return None
        return round(self.impact_ratio - self.baseline_impact_ratio, 4)

    @property
    def degrade_le_ratio(self) -> bool:
        delta = self.impact_delta
        return delta is not None and delta < -0.001

    def as_dict(self) -> dict:
        return {
            "offer": self.offer.slug,
            "weights": self.weights,
            "movements": self.mouvements,
            "impact_ratio": self.impact_ratio,
            "baseline_impact_ratio": self.baseline_impact_ratio,
            "impact_delta": self.impact_delta,
            "rows": [
                {
                    "application": str(ligne.application.pk),
                    "rank_before": ligne.rang_actuel,
                    "rank_after": ligne.rang_simule,
                    "score_after": round(ligne.score_simule, 4),
                    "delta_points": ligne.delta_points,
                }
                for ligne in self.rows
            ],
        }


def normalise(bruts: dict[str, float]) -> dict[str, float]:
    """Borne chaque poids puis ramene la somme a 1.

    Un formulaire peut envoyer n'importe quoi ; le moteur, lui, suppose une
    ponderation qui somme a 1. La normalisation est faite ici pour que le
    chiffre affiche soit celui applique.
    """
    propres = {}
    for nom in DEFAULT_WEIGHTS:
        valeur = bruts.get(nom, DEFAULT_WEIGHTS[nom])
        try:
            valeur = float(valeur)
        except (TypeError, ValueError):
            valeur = DEFAULT_WEIGHTS[nom]
        propres[nom] = min(max(valeur, POIDS_MIN), POIDS_MAX)

    total = sum(propres.values())
    if total <= 0:
        # Tout a zero : le score n'aurait plus aucun sens. On revient au defaut
        # plutot que de renvoyer une division par zero deguisee en resultat.
        return normalise(DEFAULT_WEIGHTS)

    normalises = {nom: round(valeur / total, 4) for nom, valeur in propres.items()}

    # L'arrondi laisse un residu : six poids a 1/6 arrondis a 0,1667 somment a
    # 1,0002. Le moteur renormalise de son cote, donc le score est juste — mais
    # les chiffres affiches ne feraient pas 1, et une ponderation qu'on lit
    # doit s'additionner. Le residu va au poids le plus lourd, ou il est le
    # moins visible.
    residu = round(1.0 - sum(normalises.values()), 4)
    if residu:
        plus_lourd = max(normalises, key=lambda nom: normalises[nom])
        normalises[plus_lourd] = round(normalises[plus_lourd] + residu, 4)
    return normalises


def _classer(scores: dict[str, float]) -> dict[str, int]:
    ordonnes = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return {cle: rang for rang, (cle, _) in enumerate(ordonnes, start=1)}


def simulate(
    offer: JobOffer, weights: dict[str, float], *, with_bias: bool = True
) -> Simulation:
    """Rejoue le classement de l'offre avec une autre ponderation."""
    poids = normalise(weights)
    reference = offer.weights

    candidatures = list(
        offer.applications.select_related("candidate").prefetch_related(
            "candidate__skills", "candidate__languages", "candidate__certifications"
        )
    )

    actuels: dict[str, float] = {}
    simules: dict[str, float] = {}
    par_cle: dict[str, Application] = {}
    for candidature in candidatures:
        cle = str(candidature.pk)
        par_cle[cle] = candidature
        actuels[cle] = engine.score(candidature.candidate, offer).overall
        simules[cle] = engine.score(
            candidature.candidate, offer, weights=poids
        ).overall

    rangs_actuels = _classer(actuels)
    rangs_simules = _classer(simules)

    simulation = Simulation(offer=offer, weights=poids, baseline_weights=reference)
    simulation.rows = sorted(
        (
            Ligne(
                application=par_cle[cle],
                score_actuel=actuels[cle],
                score_simule=simules[cle],
                rang_actuel=rangs_actuels[cle],
                rang_simule=rangs_simules[cle],
            )
            for cle in par_cle
        ),
        key=lambda ligne: ligne.rang_simule,
    )

    if with_bias:
        _mesurer_le_biais(simulation, poids, reference)
    return simulation


def _mesurer_le_biais(
    simulation: Simulation, poids: dict[str, float], reference: dict[str, float]
) -> None:
    """Ratio d'impact du critere identitaire, avant et apres.

    On ne mesure que la dimension que l'audit a identifiee comme influente —
    la localisation. Rejouer l'audit complet a chaque deplacement de curseur
    couterait des centaines de scorings pour trois chiffres inchanges.
    """
    from apps.evaluation import bias

    try:
        simulation.impact_dimension = bias.MOST_INFLUENTIAL_DIMENSION
        simulation.baseline_impact_ratio = bias.impact_ratio_for_weights(reference)
        simulation.impact_ratio = bias.impact_ratio_for_weights(poids)
    except Exception:  # noqa: BLE001
        # Le simulateur reste utile sans le ratio ; l'absence est visible a
        # l'ecran plutot que masquee par un chiffre invente.
        simulation.impact_ratio = None
        simulation.baseline_impact_ratio = None
