"""Audit de biais par contrefactuels.

La question posee est celle d'un auditeur, pas d'un developpeur : **si cette
meme personne avait un autre prenom, une autre ville, un autre age apparent,
serait-elle toujours retenue ?**

Methode. Pour chaque candidat d'un cas annote, on produit des variantes ne
differant que par un attribut identitaire, on rescore, et on mesure :

  - l'ecart de score cause par ce seul changement ;
  - le nombre de fois ou le classement s'en trouve modifie ;
  - le **ratio d'impact** : taux de selection en tete de classement de la
    variante la moins retenue, divise par celui de la mieux retenue.

Le ratio d'impact et son seuil de 0.80 viennent de la regle dite « des quatre
cinquiemes », reprise par la loi new-yorkaise LL144 sur l'audit des outils
automatises d'aide au recrutement. En dessous, l'ecart de traitement entre
groupes est juge significatif.

Ce module ne prouve pas l'absence de biais — aucune methode ne le peut. Il
mesure ce qui est mesurable et rend visible ce qui ne l'est pas.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from apps.candidates.models import Candidate
from apps.matching import engine

from .harness import _temporary_case, load_dataset

# Rang au-dela duquel un candidat n'est plus considere comme retenu.
SHORTLIST_SIZE = 3
# Seuil de la regle des quatre cinquiemes.
IMPACT_RATIO_THRESHOLD = 0.80
# Ecart de score au-dela duquel un attribut identitaire est juge influent.
DELTA_TOLERANCE = 0.001


@dataclass(frozen=True)
class Variant:
    """Une valeur possible d'un attribut identitaire."""

    label: str
    apply: Callable[[Candidate], None]


def _set_name(value: str) -> Callable[[Candidate], None]:
    def mutate(candidate: Candidate) -> None:
        candidate.full_name = value

    return mutate


def _set_location(value: str) -> Callable[[Candidate], None]:
    def mutate(candidate: Candidate) -> None:
        candidate.location = value

    return mutate


def _set_graduation_year(value: int) -> Callable[[Candidate], None]:
    def mutate(candidate: Candidate) -> None:
        candidate.education.update(graduation_year=value)

    return mutate


def _set_institution(value: str) -> Callable[[Candidate], None]:
    def mutate(candidate: Candidate) -> None:
        candidate.education.update(institution=value)

    return mutate


# Les prenoms et noms sont choisis pour couvrir des origines percues et des
# genres differents. Ils ne designent personne : ce sont des sondes.
DIMENSIONS: dict[str, list[Variant]] = {
    "prenom_et_nom": [
        Variant("Marc Dupont", _set_name("Marc Dupont")),
        Variant("Marie Dupont", _set_name("Marie Dupont")),
        Variant("Youssef El Amrani", _set_name("Youssef El Amrani")),
        Variant("Fatima El Amrani", _set_name("Fatima El Amrani")),
        Variant("Wei Chen", _set_name("Wei Chen")),
    ],
    "localisation": [
        Variant("Casablanca", _set_location("Casablanca")),
        Variant("Rabat", _set_location("Rabat")),
        Variant("Tanger", _set_location("Tanger")),
    ],
    "annee_de_diplome": [
        Variant("2024 (profil jeune)", _set_graduation_year(2024)),
        Variant("2010", _set_graduation_year(2010)),
        Variant("1996 (profil senior)", _set_graduation_year(1996)),
    ],
    "etablissement": [
        Variant("Grande ecole", _set_institution("Ecole Polytechnique")),
        Variant("Universite publique", _set_institution("Universite Hassan II")),
        Variant("Etablissement prive local", _set_institution("Institut prive de Fes")),
    ],
}


@dataclass
class DimensionResult:
    dimension: str
    variants: list[str]
    mean_abs_delta: float
    max_abs_delta: float
    max_delta_example: str
    rank_changes: int
    comparisons: int
    selection_rates: dict[str, float]
    impact_ratio: float

    @property
    def influences_score(self) -> bool:
        return self.max_abs_delta > DELTA_TOLERANCE

    @property
    def passes(self) -> bool:
        return self.impact_ratio >= IMPACT_RATIO_THRESHOLD


@dataclass
class PropertyCheck:
    """Propriete de non-discrimination verifiee par construction."""

    name: str
    description: str
    holds: bool
    detail: str = ""


@dataclass
class BiasReport:
    dataset: str
    engine_version: str
    shortlist_size: int
    dimensions: list[DimensionResult]
    properties: list[PropertyCheck] = field(default_factory=list)
    blind: bool = False

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "engine_version": self.engine_version,
            "shortlist_size": self.shortlist_size,
            "blind": self.blind,
            "impact_ratio_threshold": IMPACT_RATIO_THRESHOLD,
            "dimensions": [asdict(item) for item in self.dimensions],
            "properties": [asdict(item) for item in self.properties],
        }

    def dimension(self, name: str) -> DimensionResult | None:
        return next((item for item in self.dimensions if item.dimension == name), None)

    def failures(self) -> list[DimensionResult]:
        return [item for item in self.dimensions if not item.passes]

    def broken_properties(self) -> list[PropertyCheck]:
        return [item for item in self.properties if not item.holds]


# --- Audit ------------------------------------------------------------------
def audit(dataset_name: str = "ranking_v1", *, blind: bool = False) -> BiasReport:
    dataset = load_dataset(dataset_name)
    accumulators = {
        dimension: _Accumulator(dimension, variants, blind=blind)
        for dimension, variants in DIMENSIONS.items()
    }
    properties = _PropertyAccumulator(blind=blind)

    for case in dataset["cases"]:
        with _temporary_case(case) as (offer, pairs):
            candidates = [candidate for _, candidate in pairs]
            baseline = {
                candidate.pk: engine.score(candidate, offer, blind=blind).overall
                for candidate in candidates
            }
            properties.observe(offer, candidates, baseline)

            for accumulator in accumulators.values():
                accumulator.observe(offer, candidates, baseline)

    return BiasReport(
        dataset=dataset["name"],
        engine_version=engine.ENGINE_VERSION,
        shortlist_size=SHORTLIST_SIZE,
        dimensions=[accumulator.result() for accumulator in accumulators.values()],
        properties=properties.results(),
        blind=blind,
    )


@dataclass
class Mitigation:
    """Effet mesure du screening a l'aveugle, dimension par dimension."""

    dimension: str
    ratio_standard: float
    ratio_blind: float
    max_delta_standard: float
    max_delta_blind: float
    rank_changes_standard: int
    rank_changes_blind: int

    @property
    def gain(self) -> float:
        return round(self.ratio_blind - self.ratio_standard, 4)

    @property
    def neutralised(self) -> bool:
        return (
            self.max_delta_standard > DELTA_TOLERANCE
            and self.max_delta_blind <= DELTA_TOLERANCE
        )


def compare_blind(dataset_name: str = "ranking_v1") -> tuple[BiasReport, BiasReport, list[Mitigation]]:
    """Audite le moteur avec et sans screening a l'aveugle.

    C'est la mesure qui justifie — ou non — l'activation du mode aveugle sur
    une offre : elle chiffre ce que l'on gagne en equite de traitement, en
    regard de ce que l'on perd comme critere metier.
    """
    standard = audit(dataset_name, blind=False)
    blind = audit(dataset_name, blind=True)

    mitigations = []
    for reference in standard.dimensions:
        masked = blind.dimension(reference.dimension)
        if masked is None:
            continue
        mitigations.append(
            Mitigation(
                dimension=reference.dimension,
                ratio_standard=reference.impact_ratio,
                ratio_blind=masked.impact_ratio,
                max_delta_standard=reference.max_abs_delta,
                max_delta_blind=masked.max_abs_delta,
                rank_changes_standard=reference.rank_changes,
                rank_changes_blind=masked.rank_changes,
            )
        )
    return standard, blind, mitigations


class _Accumulator:
    """Cumule les observations d'une dimension sur l'ensemble des cas."""

    def __init__(
        self, dimension: str, variants: list[Variant], *, blind: bool = False
    ) -> None:
        self.dimension = dimension
        self.variants = variants
        self.blind = blind
        self.deltas: list[float] = []
        self.max_delta = 0.0
        self.max_delta_example = ""
        self.rank_changes = 0
        self.comparisons = 0
        self.selected: dict[str, int] = {variant.label: 0 for variant in variants}
        self.observed: dict[str, int] = {variant.label: 0 for variant in variants}

    def observe(self, offer, candidates: list[Candidate], baseline: dict) -> None:
        for candidate in candidates:
            reference_score = baseline[candidate.pk]
            reference_rank = _rank_of(candidate, baseline)
            snapshot = _snapshot(candidate)

            for variant in self.variants:
                variant.apply(candidate)
                score = engine.score(candidate, offer, blind=self.blind).overall

                delta = score - reference_score
                self.deltas.append(abs(delta))
                self.comparisons += 1

                if abs(delta) > abs(self.max_delta):
                    self.max_delta = delta
                    self.max_delta_example = (
                        f"{variant.label} : {reference_score:.3f} -> {score:.3f}"
                    )

                altered = dict(baseline)
                altered[candidate.pk] = score
                rank = _rank_of(candidate, altered)
                if rank != reference_rank:
                    self.rank_changes += 1

                self.observed[variant.label] += 1
                self.selected[variant.label] += int(rank <= SHORTLIST_SIZE)

                _restore(candidate, snapshot)

    def result(self) -> DimensionResult:
        rates = {
            label: (self.selected[label] / self.observed[label])
            if self.observed[label]
            else 0.0
            for label in self.selected
        }
        values = list(rates.values())
        highest = max(values) if values else 0.0
        lowest = min(values) if values else 0.0
        # Aucune selection nulle part : aucun ecart de traitement a signaler.
        ratio = 1.0 if highest == 0 else lowest / highest

        return DimensionResult(
            dimension=self.dimension,
            variants=[variant.label for variant in self.variants],
            mean_abs_delta=round(
                sum(self.deltas) / len(self.deltas) if self.deltas else 0.0, 5
            ),
            max_abs_delta=round(abs(self.max_delta), 5),
            max_delta_example=self.max_delta_example,
            rank_changes=self.rank_changes,
            comparisons=self.comparisons,
            selection_rates={label: round(value, 4) for label, value in rates.items()},
            impact_ratio=round(ratio, 4),
        )


class _PropertyAccumulator:
    """Verifie des proprietes de non-discrimination sur tous les cas."""

    def __init__(self, *, blind: bool = False) -> None:
        self.blind = blind
        self.overqualification_penalised: list[str] = []
        self.name_influenced: list[str] = []

    def observe(self, offer, candidates: list[Candidate], baseline: dict) -> None:
        for candidate in candidates:
            reference = baseline[candidate.pk]

            # Propriete 1 : ajouter de l'anciennete ne doit jamais faire baisser
            # le score. Une penalite de surqualification correlerait avec l'age.
            original_years = candidate.total_experience_years
            candidate.total_experience_years = original_years + 15
            altered = engine.score(candidate, offer, blind=self.blind).overall
            if altered < reference - DELTA_TOLERANCE:
                self.overqualification_penalised.append(candidate.full_name)
            candidate.total_experience_years = original_years

            # Propriete 2 : le nom ne doit avoir aucun effet, au bit pres.
            original_name = candidate.full_name
            candidate.full_name = "Zzz Zzz"
            renamed = engine.score(candidate, offer, blind=self.blind).overall
            if abs(renamed - reference) > 0:
                self.name_influenced.append(original_name)
            candidate.full_name = original_name

    def results(self) -> list[PropertyCheck]:
        return [
            PropertyCheck(
                name="nom_sans_effet",
                description=(
                    "Le nom du candidat n'entre dans aucun critere : changer le "
                    "nom ne change pas le score, a la decimale pres."
                ),
                holds=not self.name_influenced,
                detail=(
                    ""
                    if not self.name_influenced
                    else f"{len(self.name_influenced)} candidat(s) affecte(s)"
                ),
            ),
            PropertyCheck(
                name="surqualification_non_penalisee",
                description=(
                    "Ajouter quinze ans d'anciennete ne fait jamais baisser le "
                    "score. Penaliser la surqualification serait un critere "
                    "d'age indirect."
                ),
                holds=not self.overqualification_penalised,
                detail=(
                    ""
                    if not self.overqualification_penalised
                    else f"{len(self.overqualification_penalised)} candidat(s) penalise(s)"
                ),
            ),
        ]


# --- Utilitaires ------------------------------------------------------------
def _rank_of(candidate: Candidate, scores: dict) -> int:
    """Rang du candidat, 1 = meilleur. Les ex aequo prennent le meilleur rang."""
    value = scores[candidate.pk]
    return 1 + sum(1 for other in scores.values() if other > value)


def _snapshot(candidate: Candidate) -> dict:
    return {
        "full_name": candidate.full_name,
        "location": candidate.location,
        "education": list(
            candidate.education.values_list("pk", "institution", "graduation_year")
        ),
    }


def _restore(candidate: Candidate, snapshot: dict) -> None:
    candidate.full_name = snapshot["full_name"]
    candidate.location = snapshot["location"]
    for pk, institution, year in snapshot["education"]:
        candidate.education.filter(pk=pk).update(
            institution=institution, graduation_year=year
        )
