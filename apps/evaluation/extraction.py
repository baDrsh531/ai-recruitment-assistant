"""Harnais d'evaluation de l'extraction des CV.

Le harnais de classement mesure le moteur deterministe. Celui-ci mesure la
partie confiee au modele de langage : ce qu'il retrouve dans un CV, ce qu'il
manque, et ce qu'il invente.

Le protocole evite l'annotation manuelle : un profil structure est mis en page
par `cv_factory`, le systeme reconstitue ce profil a partir du PDF, et le
resultat est compare a la source. La verite terrain est donc exacte par
construction — mais les CV produits sont plus propres que les vrais, et les
scores obtenus sont optimistes d'autant.

Contrairement au harnais de classement, celui-ci **appelle reellement le
modele** : il ne tourne pas en integration continue et demande un serveur
d'inference joignable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from apps.candidates.models import CVDocument
from apps.matching.ontology import normalize as normalize_skill
from apps.parsing.services import ingest

from . import cv_factory, metrics

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"

# Seuils de non-regression. Volontairement moins exigeants que ceux du
# classement : l'extraction depend d'un modele, pas d'un calcul.
THRESHOLDS = {
    "identity_accuracy": 0.80,
    "skills_f1": 0.80,
    "languages_f1": 0.75,
    "evidence_anchored": 0.85,
    "experience_years_mae": 1.5,  # a l'envers : plus bas vaut mieux
}
LOWER_IS_BETTER = {"experience_years_mae"}

# Ecart tolere sur l'anciennete totale reconstituee, en annees.
YEARS_TOLERANCE = 1.5


@dataclass
class CaseResult:
    id: str
    layout: str
    method: str
    seconds: float
    identity_accuracy: float
    identity_detail: dict[str, bool] = field(default_factory=dict)
    skills: dict[str, float] = field(default_factory=dict)
    languages: dict[str, float] = field(default_factory=dict)
    experiences_expected: int = 0
    experiences_found: int = 0
    education_expected: int = 0
    education_found: int = 0
    experience_years_expected: float = 0.0
    experience_years_found: float = 0.0
    evidence_total: int = 0
    evidence_anchored: int = 0
    evidence_verifiable: bool = True
    missed_skills: list[str] = field(default_factory=list)
    invented_skills: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def experience_years_error(self) -> float:
        return abs(self.experience_years_found - self.experience_years_expected)

    @property
    def evidence_ratio(self) -> float:
        return self.evidence_anchored / self.evidence_total if self.evidence_total else 1.0


@dataclass
class Report:
    dataset: str
    dataset_version: str
    aggregate: dict[str, float]
    cases: list[CaseResult]

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "aggregate": self.aggregate,
            "thresholds": THRESHOLDS,
            "cases": [asdict(case) for case in self.cases],
        }

    def failures(self) -> dict[str, tuple[float, float]]:
        failed = {}
        for name, threshold in THRESHOLDS.items():
            value = self.aggregate.get(name)
            if value is None:
                continue
            breached = value > threshold if name in LOWER_IS_BETTER else value < threshold
            if breached:
                failed[name] = (value, threshold)
        return failed


# --- Chargement -------------------------------------------------------------
KIND = "extraction"


def load_dataset(name: str = "extraction_v1") -> dict:
    path = DATASETS_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Jeu d'evaluation introuvable : {name}")
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if dataset.get("kind") != KIND:
        raise ValueError(
            f"« {name} » est un jeu de type « {dataset.get('kind') or 'inconnu'} », "
            f"attendu « {KIND} ». Pour le classement, utiliser "
            "`python manage.py evaluate`."
        )
    return dataset


# --- Execution --------------------------------------------------------------
def run_case(case: dict) -> CaseResult:
    """Genere le CV, le fait extraire, compare au profil d'origine."""
    profile = case["profile"]
    layout = case.get("layout", "simple")
    pdf = cv_factory.build(profile, layout)

    with transaction.atomic():
        try:
            document, _ = ingest(
                SimpleUploadedFile(
                    f"{case['id']}_{layout}.pdf", pdf, content_type="application/pdf"
                )
            )
            document.refresh_from_db()
            result = _compare(case, document)
        except Exception as exc:  # noqa: BLE001
            result = CaseResult(
                id=case["id"], layout=layout, method="", seconds=0.0,
                identity_accuracy=0.0, error=str(exc)[:300],
            )
        finally:
            # Evaluer ne laisse aucune trace, comme pour le classement.
            transaction.set_rollback(True)
    return result


def _compare(case: dict, document: CVDocument) -> CaseResult:
    profile = case["profile"]
    candidate = document.candidate

    identity = {
        "full_name": _same_text(candidate.full_name, profile["full_name"]),
        "email": candidate.email.lower() == profile["email"].lower(),
        "phone": _digits(candidate.phone) == _digits(profile.get("phone", "")),
        "location": _same_text(candidate.location, profile.get("location", "")),
        "headline": _loose_text(candidate.headline, profile.get("headline", "")),
    }

    expected_skills = {normalize_skill(name) for name in profile.get("skills", [])}
    found_skills = {
        normalize_skill(skill.name) for skill in candidate.skills.all()
    }

    expected_languages = {
        _strip(item["language"]) for item in profile.get("languages", [])
    }
    found_languages = {
        _strip(item.language) for item in candidate.languages.all()
    }

    spans = list(document.spans.all())

    return CaseResult(
        id=case["id"],
        layout=case.get("layout", "simple"),
        method=document.method,
        seconds=round(document.extraction_seconds or 0.0, 2),
        identity_accuracy=sum(identity.values()) / len(identity),
        identity_detail=identity,
        skills=metrics.set_prf(found_skills, expected_skills),
        languages=metrics.set_prf(found_languages, expected_languages),
        experiences_expected=len(profile.get("experiences", [])),
        experiences_found=candidate.experiences.count(),
        education_expected=len(profile.get("education", [])),
        education_found=candidate.education.count(),
        experience_years_expected=round(case.get("expected_years", 0.0), 2),
        experience_years_found=round(candidate.total_experience_years, 2),
        evidence_total=len(spans),
        evidence_anchored=sum(1 for span in spans if span.verified),
        evidence_verifiable=document.evidence_verifiable,
        missed_skills=sorted(expected_skills - found_skills),
        invented_skills=sorted(found_skills - expected_skills),
    )


def run(dataset_name: str = "extraction_v1") -> Report:
    dataset = load_dataset(dataset_name)
    cases = [run_case(case) for case in dataset["cases"]]
    usable = [case for case in cases if not case.error]

    if not usable:
        aggregate = {}
    else:
        # L'ancrage n'est mesure que la ou il est possible : sur un document
        # scanne, il n'existe aucune couche texte a confronter. L'inclure
        # ferait passer une limite connue du format pour un defaut du systeme.
        verifiables = [case for case in usable if case.evidence_verifiable]
        aggregate = {
            "identity_accuracy": _mean(case.identity_accuracy for case in usable),
            "skills_f1": _mean(case.skills["f1"] for case in usable),
            "skills_precision": _mean(case.skills["precision"] for case in usable),
            "skills_recall": _mean(case.skills["recall"] for case in usable),
            "languages_f1": _mean(case.languages["f1"] for case in usable),
            "evidence_anchored": _mean(case.evidence_ratio for case in verifiables),
            "experience_years_mae": _mean(
                case.experience_years_error for case in usable
            ),
            "seconds_mean": _mean(case.seconds for case in usable),
            "cases_failed": len(cases) - len(usable),
            "cases_unverifiable": len(usable) - len(verifiables),
        }

    return Report(
        dataset=dataset["name"],
        dataset_version=dataset.get("version", "0"),
        aggregate=aggregate,
        cases=cases,
    )


# --- Comparaisons -----------------------------------------------------------
def _strip(text: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _same_text(left: str, right: str) -> bool:
    return _strip(left) == _strip(right)


def _loose_text(left: str, right: str) -> bool:
    """Le titre professionnel se reformule : on accepte l'inclusion."""
    a, b = _strip(left), _strip(right)
    if not a or not b:
        return a == b
    return a in b or b in a


def _digits(text: str) -> str:
    return "".join(char for char in (text or "") if char.isdigit())


def _mean(values) -> float:
    listed = list(values)
    return round(sum(listed) / len(listed), 4) if listed else 0.0
