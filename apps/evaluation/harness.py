"""Harnais d'evaluation du moteur de classement.

Le jeu d'evaluation contient des cas annotes a la main : une offre, des
candidats, et pour chacun une note de pertinence attribuee par un humain. Le
harnais reconstruit ces cas en base, fait tourner le moteur, compare le
classement produit au classement attendu et renvoie des chiffres.

Les objets sont crees dans une transaction systematiquement annulee : evaluer
ne laisse aucune trace en base.

C'est ce module qui permet de repondre a « est-ce que ma modification a
ameliore quelque chose ? » autrement que par une impression.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from django.db import transaction

from apps.candidates.models import (
    Candidate,
    CandidateLanguage,
    CandidateSkill,
    Certification,
)
from apps.jobs.models import JobLanguage, JobOffer, JobSkill
from apps.matching import engine

from . import metrics

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"

# Seuils de non-regression. Les abaisser doit etre un acte conscient, discute
# en revue — pas un ajustement silencieux pour faire passer la CI.
THRESHOLDS = {
    "ndcg_at_5": 0.90,
    "precision_at_3": 0.85,
    "pair_accuracy": 0.85,
    "spearman": 0.75,
}


@dataclass
class CaseResult:
    id: str
    ndcg_at_5: float
    precision_at_3: float
    pair_accuracy: float
    spearman: float
    predicted_order: list[str] = field(default_factory=list)
    relevances: list[int] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)


@dataclass
class Report:
    dataset: str
    dataset_version: str
    engine_version: str
    semantic_used: bool
    cases: list[CaseResult]
    aggregate: dict[str, float]

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "engine_version": self.engine_version,
            "semantic_used": self.semantic_used,
            "aggregate": self.aggregate,
            "cases": [asdict(case) for case in self.cases],
        }

    def failures(self) -> dict[str, tuple[float, float]]:
        """Metriques sous leur seuil : {nom: (obtenu, seuil)}."""
        return {
            name: (self.aggregate[name], threshold)
            for name, threshold in THRESHOLDS.items()
            if name in self.aggregate and self.aggregate[name] < threshold
        }


# --- Chargement -------------------------------------------------------------
# Le dossier `datasets/` heberge des jeux de natures differentes : classement
# et extraction. Chaque fichier declare la sienne, faute de quoi la commande
# `evaluate` lirait un jeu d'extraction comme un jeu de classement et
# echouerait sur une cle absente.
KIND = "ranking"


def _kind_of(path: Path) -> str:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("kind", "")
    except (OSError, json.JSONDecodeError):
        return ""


def available_datasets() -> list[str]:
    return sorted(
        path.stem for path in DATASETS_DIR.glob("*.json") if _kind_of(path) == KIND
    )


def load_dataset(name: str) -> dict:
    path = DATASETS_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"Jeu d'evaluation introuvable : {name}. "
            f"Disponibles : {', '.join(available_datasets()) or 'aucun'}"
        )
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if dataset.get("kind") != KIND:
        raise ValueError(
            f"« {name} » est un jeu de type « {dataset.get('kind') or 'inconnu'} », "
            f"attendu « {KIND} ». Pour l'extraction, utiliser "
            "`python manage.py evaluate_extraction`."
        )
    return dataset


# --- Construction ephemere --------------------------------------------------
@contextmanager
def _temporary_case(case: dict):
    """Cree l'offre et les candidats du cas, puis annule tout.

    Les objets sont relus avec `prefetch_related` : le moteur appelle
    `candidate.skills.all()` a chaque scoring, et l'audit de biais rescore le
    meme candidat des dizaines de fois. Sans prechargement, chaque appel
    repartait en base — quelques milliers de requetes pour un seul audit.
    """
    with transaction.atomic():
        try:
            offer = _build_offer(case["offer"])
            specs = case["candidates"]
            built = [_build_candidate(spec) for spec in specs]

            offer = (
                JobOffer.objects.prefetch_related("skills", "languages")
                .get(pk=offer.pk)
            )
            loaded = {
                candidate.pk: candidate
                for candidate in Candidate.objects.filter(
                    pk__in=[item.pk for item in built]
                ).prefetch_related("skills", "languages", "certifications")
            }
            yield offer, [
                (spec, loaded[item.pk]) for spec, item in zip(specs, built, strict=True)
            ]
        finally:
            # Le harnais mesure, il ne modifie pas les donnees de travail.
            transaction.set_rollback(True)


def _build_offer(spec: dict) -> JobOffer:
    offer = JobOffer.objects.create(
        title=spec["title"],
        description=spec.get("description", ""),
        location=spec.get("location", ""),
        remote_policy=spec.get("remote_policy", JobOffer.RemotePolicy.ONSITE),
        experience_min_years=spec.get("experience_min_years", 0),
        education_level=spec.get("education_level", 0),
        required_certifications=spec.get("required_certifications", []),
        scoring_weights=spec.get("scoring_weights", {}),
        status=JobOffer.Status.OPEN,
    )
    for skill in spec.get("required_skills", []):
        JobSkill.objects.create(
            offer=offer,
            name=skill["name"],
            weight=skill.get("weight", 1.0),
            min_years=skill.get("min_years", 0),
            requirement=JobSkill.Requirement.REQUIRED,
        )
    for skill in spec.get("preferred_skills", []):
        JobSkill.objects.create(
            offer=offer,
            name=skill["name"],
            weight=skill.get("weight", 1.0),
            requirement=JobSkill.Requirement.PREFERRED,
        )
    for language in spec.get("languages", []):
        JobLanguage.objects.create(
            offer=offer,
            language=language["language"],
            min_level=language["min_level"],
            is_required=language.get("is_required", True),
        )
    return offer


def _build_candidate(spec: dict) -> Candidate:
    candidate = Candidate.objects.create(
        full_name=spec.get("name", spec["id"]),
        email=f"{spec['id']}@evaluation.local",
        total_experience_years=spec.get("years", 0.0),
        highest_education=spec.get("education", 0),
        location=spec.get("location", ""),
    )
    for skill in spec.get("skills", []):
        CandidateSkill.objects.create(
            candidate=candidate,
            name=skill["name"],
            years=skill.get("years", 0.0),
            last_used_year=skill.get("last_used_year"),
        )
    for language in spec.get("languages", []):
        CandidateLanguage.objects.create(
            candidate=candidate, language=language["language"], level=language["level"]
        )
    for certification in spec.get("certifications", []):
        Certification.objects.create(candidate=candidate, name=certification)
    return candidate


# --- Execution --------------------------------------------------------------
def run_case(case: dict) -> CaseResult:
    with _temporary_case(case) as (offer, pairs):
        scored = []
        semantic = False
        for spec, candidate in pairs:
            result = engine.score(candidate, offer)
            semantic = semantic or result.semantic_used
            scored.append((spec["id"], spec["relevance"], result.overall))

        # Ordre predit par le moteur, du meilleur au moins bon.
        scored.sort(key=lambda item: item[2], reverse=True)

    relevances = [relevance for _, relevance, _ in scored]
    scores = [round(value, 4) for _, _, value in scored]

    return CaseResult(
        id=case["id"],
        ndcg_at_5=round(metrics.ndcg_at_k(relevances, 5), 4),
        precision_at_3=round(metrics.precision_at_k(relevances, 3), 4),
        pair_accuracy=round(metrics.pair_accuracy(relevances), 4),
        spearman=round(metrics.spearman(scores, relevances), 4),
        predicted_order=[identifier for identifier, _, _ in scored],
        relevances=relevances,
        scores=scores,
    )


def run(dataset_name: str) -> Report:
    dataset = load_dataset(dataset_name)
    cases = [run_case(case) for case in dataset["cases"]]

    if not cases:
        raise ValueError(f"Le jeu {dataset_name} ne contient aucun cas.")

    aggregate = {
        name: round(sum(getattr(case, name) for case in cases) / len(cases), 4)
        for name in ("ndcg_at_5", "precision_at_3", "pair_accuracy", "spearman")
    }

    from apps.ai import embeddings

    return Report(
        dataset=dataset["name"],
        dataset_version=dataset.get("version", "0"),
        engine_version=engine.ENGINE_VERSION,
        semantic_used=embeddings.get_embedder_or_none() is not None,
        cases=cases,
        aggregate=aggregate,
    )


def compare(current: Report, baseline: dict) -> dict[str, dict[str, float]]:
    """Ecart metrique par metrique avec un rapport de reference."""
    previous = baseline.get("aggregate", {})
    return {
        name: {
            "baseline": previous[name],
            "current": value,
            "delta": round(value - previous[name], 4),
        }
        for name, value in current.aggregate.items()
        if name in previous
    }
