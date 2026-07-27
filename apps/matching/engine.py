"""Moteur de score de compatibilite — deterministe.

Aucun modele de langage n'intervient ici. Le score est le resultat d'un calcul
explicite a partir de poids configurables : deux executions sur les memes
donnees produisent le meme chiffre, chaque composante est inspectable, et le
tout est testable unitairement. Le LLM n'entre en jeu qu'ensuite, dans
`explain.py`, pour commenter un resultat deja fige.

Principe de renormalisation : un critere sur lequel l'offre n'exprime aucune
exigence est declare non applicable et exclu du calcul, les poids restants
etant renormalises. Sans cela, une offre muette sur les langues verrait 10 %
de son score attribues arbitrairement.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import asdict, dataclass, field

from apps.candidates.models import Candidate, CandidateSkill
from apps.jobs.models import LANGUAGE_LEVEL_ORDER, JobOffer, JobSkill

from . import ontology

logger = logging.getLogger(__name__)

# Toute evolution du calcul doit incrementer cette version : elle est stockee
# avec chaque score et permet de savoir quel moteur a produit quelle decision.
#
# 1.2.0 — mode aveugle : exclusion du critere de localisation.
# 1.1.0 — ajout du facteur de recevabilite (voir `_admissibility`).
# 1.0.0 — version initiale.
ENGINE_VERSION = "1.2.0"

# En dessous de cette similarite cosinus, deux intitules sont juges sans rapport.
SEMANTIC_FLOOR = 0.70
# Un rapprochement semantique ne vaut jamais autant qu'une correspondance
# exacte ou qu'une implication explicite de l'ontologie.
SEMANTIC_CEILING = 0.80
# En deca, une competence requise est comptee comme un ecart.
GAP_THRESHOLD = 0.50
# Part du score competences revenant aux competences souhaitees.
PREFERRED_SHARE = 0.15

# Recevabilite : les competences obligatoires ne sont pas un critere parmi
# d'autres, elles conditionnent le reste. Sans ce garde-fou, un profil sans
# aucune competence attendue mais bien situe, bien diplome et polyglotte
# atteignait 53 % — un chiffre que le harnais d'evaluation a rendu visible et
# qu'aucun recruteur n'accepterait. Le facteur vaut ADMISSIBILITY_FLOOR quand
# aucune competence obligatoire n'est couverte, et 1.0 des la moitie couverte.
ADMISSIBILITY_FLOOR = 0.20
ADMISSIBILITY_FULL_AT = 0.50


@dataclass(slots=True)
class SkillMatch:
    required: str
    requirement: str
    weight: float
    matched_with: str | None
    method: str  # exact | ontologie | semantique | aucun
    similarity: float
    coverage: float
    recency: float
    score: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Criterion:
    name: str
    label: str
    score: float
    applicable: bool
    detail: dict = field(default_factory=dict)
    # Poids effectivement applique, apres exclusion des criteres non
    # applicables et renormalisation. Renseigne par `score()`.
    weight: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ScoreResult:
    overall: float
    criteria: list[Criterion]
    weights_used: dict[str, float]
    skill_matches: list[SkillMatch]
    gaps: list[dict]
    engine_version: str
    semantic_used: bool
    # Moyenne ponderee avant application du facteur de recevabilite.
    weighted_score: float = 0.0
    admissibility: float = 1.0
    blind: bool = False

    def breakdown(self) -> dict:
        return {
            "overall": self.overall,
            "weighted_score": self.weighted_score,
            "admissibility": self.admissibility,
            "blind": self.blind,
            "engine_version": self.engine_version,
            "semantic_used": self.semantic_used,
            "criteria": [criterion.as_dict() for criterion in self.criteria],
            "weights_used": self.weights_used,
        }

    def criterion(self, name: str) -> Criterion | None:
        return next((c for c in self.criteria if c.name == name), None)


# --- Point d'entree ---------------------------------------------------------
def score(
    candidate: Candidate, offer: JobOffer, *, blind: bool | None = None
) -> ScoreResult:
    """Calcule le score de compatibilite d'un candidat pour une offre.

    `blind` active le screening a l'aveugle : le critere de localisation est
    exclu du calcul et ses poids sont redistribues. C'est le seul critere du
    moteur porteur d'un signal identitaire mesurable — l'audit de biais lui
    attribue un ratio d'impact de 0.809, juste au-dessus du seuil legal de
    0.80. Le desactiver ramene ce ratio a 1.0, au prix de la perte d'une
    contrainte metier reelle pour les postes sur site.

    Par defaut, la politique est celle de l'offre : le score doit etre le meme
    pour tous les recruteurs qui consultent la meme offre.
    """
    if blind is None:
        blind = offer.blind_screening

    candidate_skills = list(candidate.skills.all())
    job_skills = list(offer.skills.all())

    matcher = SkillMatcher(job_skills, candidate_skills)
    matches = [matcher.match(job_skill) for job_skill in job_skills]

    criteria = [
        _skills_criterion(job_skills, matches),
        _experience_criterion(candidate, offer),
        _education_criterion(candidate, offer),
        _languages_criterion(candidate, offer),
        _certifications_criterion(candidate, offer),
        _location_criterion(candidate, offer, blind=blind),
    ]

    weights = offer.weights
    applicable = [criterion for criterion in criteria if criterion.applicable]
    total_weight = sum(weights.get(c.name, 0.0) for c in applicable)

    if total_weight <= 0:
        weighted = 0.0
        weights_used: dict[str, float] = {}
    else:
        weights_used = {
            c.name: round(weights.get(c.name, 0.0) / total_weight, 4) for c in applicable
        }
        for criterion in applicable:
            criterion.weight = weights_used[criterion.name]
        weighted = sum(weights_used[c.name] * c.score for c in applicable)

    admissibility = _admissibility(criteria[0])
    overall = weighted * admissibility

    gaps = [
        {
            "skill": match.required,
            "requirement": match.requirement,
            "weight": match.weight,
            "best_match": match.matched_with,
            "score": match.score,
        }
        for match in matches
        if match.requirement == JobSkill.Requirement.REQUIRED and match.score < GAP_THRESHOLD
    ]

    return ScoreResult(
        overall=round(overall, 4),
        criteria=criteria,
        weights_used=weights_used,
        skill_matches=matches,
        gaps=gaps,
        engine_version=ENGINE_VERSION,
        semantic_used=matcher.semantic_used,
        weighted_score=round(weighted, 4),
        admissibility=round(admissibility, 4),
        blind=blind,
    )


def _admissibility(skills: Criterion) -> float:
    """Facteur multiplicatif traduisant la couverture des competences obligatoires.

    Les autres criteres decrivent un candidat ; les competences obligatoires
    decident s'il est dans le sujet. Un profil qui n'en couvre aucune ne doit
    pas remonter parce qu'il habite la bonne ville et parle trois langues.

    Rampe lineaire : ADMISSIBILITY_FLOOR a couverture nulle, 1.0 des
    ADMISSIBILITY_FULL_AT. Au-dela, aucun effet — le facteur ne peut que
    penaliser un profil hors sujet, jamais avantager un profil deja retenu.
    """
    if not skills.applicable:
        return 1.0
    covered = skills.detail.get("required_score")
    if covered is None or not skills.detail.get("required_count"):
        return 1.0
    ramp = ADMISSIBILITY_FLOOR + (1 - ADMISSIBILITY_FLOOR) * (
        covered / ADMISSIBILITY_FULL_AT
    )
    return min(1.0, ramp)


# --- Competences ------------------------------------------------------------
class SkillMatcher:
    """Rapproche les competences attendues de celles du candidat.

    Trois niveaux, du plus fiable au moins fiable :
      1. correspondance exacte apres normalisation (alias compris) ;
      2. ontologie : implication ou voisinage declare ;
      3. embeddings : similarite semantique, plafonnee.

    Le niveau 3 est facultatif — si aucun fournisseur d'embeddings n'est
    disponible, le moteur fonctionne en degrade et le signale via
    `semantic_used`, plutot que d'echouer.
    """

    def __init__(
        self, job_skills: list[JobSkill], candidate_skills: list[CandidateSkill]
    ) -> None:
        self.candidate_skills = candidate_skills
        self.semantic_used = False
        self._similarity: dict[tuple[str, str], float] = {}

        if job_skills and candidate_skills:
            self._precompute_semantic(job_skills, candidate_skills)

    def _precompute_semantic(
        self, job_skills: list[JobSkill], candidate_skills: list[CandidateSkill]
    ) -> None:
        """Calcule en un seul lot toutes les similarites d'intitules."""
        from apps.ai import embeddings

        embedder = embeddings.get_embedder_or_none()
        if embedder is None:
            return

        try:
            import numpy as np

            job_names = [skill.name for skill in job_skills]
            candidate_names = [skill.name for skill in candidate_skills]
            vectors = embedder.encode(job_names + candidate_names)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Echec du calcul d'embeddings : %s", exc)
            return

        split = len(job_skills)
        job_vectors, candidate_vectors = vectors[:split], vectors[split:]
        matrix = job_vectors @ candidate_vectors.T
        for i, job_skill in enumerate(job_skills):
            for j, candidate_skill in enumerate(candidate_skills):
                self._similarity[(job_skill.name, candidate_skill.name)] = float(
                    np.clip(matrix[i][j], 0.0, 1.0)
                )
        self.semantic_used = True

    def match(self, job_skill: JobSkill) -> SkillMatch:
        best_score, best_skill, best_method, best_similarity = 0.0, None, "aucun", 0.0

        for candidate_skill in self.candidate_skills:
            related = ontology.relatedness(job_skill.name, candidate_skill.name)
            if related >= 1.0:
                method, value = "exact", related
            elif related > 0:
                method, value = "ontologie", related
            else:
                method, value = "aucun", 0.0

            cosine = self._similarity.get((job_skill.name, candidate_skill.name), 0.0)
            if cosine > SEMANTIC_FLOOR:
                semantic = SEMANTIC_CEILING * (cosine - SEMANTIC_FLOOR) / (1 - SEMANTIC_FLOOR)
                if semantic > value:
                    method, value = "semantique", min(semantic, SEMANTIC_CEILING)

            if value > best_score:
                best_score, best_skill, best_method = value, candidate_skill, method
                best_similarity = cosine

        if best_skill is None:
            return SkillMatch(
                required=job_skill.name,
                requirement=job_skill.requirement,
                weight=job_skill.weight,
                matched_with=None,
                method="aucun",
                similarity=0.0,
                coverage=0.0,
                recency=0.0,
                score=0.0,
            )

        coverage = _coverage_factor(job_skill.min_years, best_skill.years)
        recency = _recency_factor(best_skill)

        return SkillMatch(
            required=job_skill.name,
            requirement=job_skill.requirement,
            weight=job_skill.weight,
            matched_with=best_skill.name,
            method=best_method,
            similarity=round(best_similarity, 3),
            coverage=round(coverage, 3),
            recency=round(recency, 3),
            score=round(best_score * coverage * recency, 4),
        )


def _coverage_factor(required_years: int, candidate_years: float) -> float:
    """Penalite d'anciennete insuffisante, bornee a 0.70.

    Une anciennete inconnue (frequente : peu de CV la precisent competence par
    competence) ne doit pas etre traitee comme une anciennete nulle.
    """
    if required_years <= 0:
        return 1.0
    if candidate_years <= 0:
        return 0.85
    if candidate_years >= required_years:
        return 1.0
    return 0.70 + 0.30 * (candidate_years / required_years)


def _recency_factor(skill: CandidateSkill) -> float:
    """Ramene le facteur de fraicheur [0.55, 1.0] dans [0.80, 1.0].

    Une competence ancienne compte moins, mais elle ne s'efface pas : un
    developpeur qui n'a pas touche a Java depuis six ans le sait toujours.
    """
    return 0.80 + 0.20 * (skill.recency_factor - 0.55) / 0.45


def _skills_criterion(job_skills: list[JobSkill], matches: list[SkillMatch]) -> Criterion:
    required = [m for m in matches if m.requirement == JobSkill.Requirement.REQUIRED]
    preferred = [m for m in matches if m.requirement == JobSkill.Requirement.PREFERRED]

    if not job_skills:
        return Criterion("skills", "Competences", 0.0, applicable=False)

    required_score = _weighted_mean(required)
    preferred_score = _weighted_mean(preferred)

    if required and preferred:
        value = (1 - PREFERRED_SHARE) * required_score + PREFERRED_SHARE * preferred_score
    elif required:
        value = required_score
    else:
        value = preferred_score

    return Criterion(
        name="skills",
        label="Competences",
        score=round(value, 4),
        applicable=True,
        detail={
            "required_score": round(required_score, 4),
            "preferred_score": round(preferred_score, 4),
            "required_count": len(required),
            "preferred_count": len(preferred),
            "matched": sum(1 for m in required if m.score >= GAP_THRESHOLD),
        },
    )


def _weighted_mean(matches: list[SkillMatch]) -> float:
    total_weight = sum(match.weight for match in matches)
    if total_weight <= 0:
        return 0.0
    return sum(match.weight * match.score for match in matches) / total_weight


# --- Experience -------------------------------------------------------------
def _experience_criterion(candidate: Candidate, offer: JobOffer) -> Criterion:
    required = offer.experience_min_years
    years = candidate.total_experience_years

    if required <= 0:
        return Criterion(
            "experience", "Experience", 0.0, applicable=False,
            detail={"reason": "aucune anciennete minimale exigee"},
        )

    ratio = years / required
    # Courbe adoucie : a 50 % de l'anciennete demandee le score vaut 0.62, pas
    # 0.50. Un seuil brutal ecarterait des profils proches sans justification.
    value = 1.0 if ratio >= 1 else max(0.0, ratio) ** 0.7

    # Pas de penalite de surqualification : elle correlerait avec l'age et
    # constituerait un critere discriminatoire indirect.
    return Criterion(
        name="experience",
        label="Experience",
        score=round(min(value, 1.0), 4),
        applicable=True,
        detail={
            "candidate_years": round(years, 2),
            "required_years": required,
            "ratio": round(ratio, 3),
        },
    )


# --- Formation --------------------------------------------------------------
def _education_criterion(candidate: Candidate, offer: JobOffer) -> Criterion:
    required = offer.education_level
    obtained = candidate.highest_education

    if not required:
        return Criterion(
            "education", "Formation", 0.0, applicable=False,
            detail={"reason": "aucun niveau d'etudes exige"},
        )

    value = 1.0 if obtained >= required else max(0.0, obtained / required)
    return Criterion(
        name="education",
        label="Formation",
        score=round(value, 4),
        applicable=True,
        detail={"candidate_level": obtained, "required_level": required},
    )


# --- Langues ----------------------------------------------------------------
def _languages_criterion(candidate: Candidate, offer: JobOffer) -> Criterion:
    expected = list(offer.languages.all())
    if not expected:
        return Criterion(
            "languages", "Langues", 0.0, applicable=False,
            detail={"reason": "aucune langue exigee"},
        )

    spoken = {
        _strip_accents(language.language.lower()): LANGUAGE_LEVEL_ORDER.get(language.level, 0)
        for language in candidate.languages.all()
    }

    scores: list[tuple[float, float]] = []  # (poids, score)
    detail: list[dict] = []
    for requirement in expected:
        key = _strip_accents(requirement.language.lower())
        needed = LANGUAGE_LEVEL_ORDER.get(requirement.min_level, 0)
        actual = spoken.get(key, 0)

        if actual == 0:
            value = 0.0
        elif actual >= needed:
            value = 1.0
        else:
            # Chaque palier CECRL manquant coute 25 %.
            value = max(0.0, 1.0 - 0.25 * (needed - actual))

        weight = 1.0 if requirement.is_required else 0.5
        scores.append((weight, value))
        detail.append(
            {
                "language": requirement.language,
                "required": requirement.min_level,
                "candidate_level": actual,
                "required_order": needed,
                "score": round(value, 3),
                "is_required": requirement.is_required,
            }
        )

    total_weight = sum(weight for weight, _ in scores)
    value = sum(weight * item for weight, item in scores) / total_weight

    return Criterion(
        name="languages", label="Langues", score=round(value, 4),
        applicable=True, detail={"languages": detail},
    )


# --- Certifications ---------------------------------------------------------
def _certifications_criterion(candidate: Candidate, offer: JobOffer) -> Criterion:
    expected = [name for name in (offer.required_certifications or []) if name.strip()]
    if not expected:
        return Criterion(
            "certifications", "Certifications", 0.0, applicable=False,
            detail={"reason": "aucune certification exigee"},
        )

    held = [_strip_accents(cert.name.lower()) for cert in candidate.certifications.all()]
    detail = []
    matched = 0
    for name in expected:
        needle = _strip_accents(name.lower())
        found = any(needle in item or item in needle for item in held)
        matched += int(found)
        detail.append({"certification": name, "held": found})

    return Criterion(
        name="certifications",
        label="Certifications",
        score=round(matched / len(expected), 4),
        applicable=True,
        detail={"certifications": detail},
    )


# --- Localisation -----------------------------------------------------------
def _location_criterion(
    candidate: Candidate, offer: JobOffer, *, blind: bool = False
) -> Criterion:
    if blind:
        return Criterion(
            "location", "Localisation", 0.0, applicable=False,
            detail={"reason": "screening a l'aveugle : critere exclu"},
        )
    if offer.remote_policy == JobOffer.RemotePolicy.REMOTE:
        return Criterion(
            "location", "Localisation", 0.0, applicable=False,
            detail={"reason": "poste en teletravail total"},
        )
    if not offer.location.strip():
        return Criterion(
            "location", "Localisation", 0.0, applicable=False,
            detail={"reason": "localisation non precisee"},
        )
    if not candidate.location.strip():
        return Criterion(
            "location", "Localisation", 0.0, applicable=False,
            detail={"reason": "localisation du candidat inconnue"},
        )

    # Heuristique de comparaison textuelle. Un geocodage (distance reelle,
    # temps de trajet) serait plus juste ; c'est une amelioration identifiee.
    offer_tokens = _place_tokens(offer.location)
    candidate_tokens = _place_tokens(candidate.location)
    shared = offer_tokens & candidate_tokens

    if shared:
        value = 1.0
    elif offer.remote_policy == JobOffer.RemotePolicy.HYBRID:
        value = 0.45
    else:
        value = 0.25

    return Criterion(
        name="location",
        label="Localisation",
        score=value,
        applicable=True,
        detail={
            "offer": offer.location,
            "candidate": candidate.location,
            "shared_tokens": sorted(shared),
            "remote_policy": offer.remote_policy,
            "method": "heuristique textuelle",
        },
    )


# --- Utilitaires ------------------------------------------------------------
def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _place_tokens(place: str) -> set[str]:
    cleaned = _strip_accents(place.lower())
    for separator in (",", "/", "-", "(", ")"):
        cleaned = cleaned.replace(separator, " ")
    return {token for token in cleaned.split() if len(token) > 2}
