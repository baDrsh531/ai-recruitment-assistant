"""Comparaison de plusieurs candidats sur une meme offre.

Un classement dit qui arrive devant ; il ne dit pas pourquoi, ni sur quoi les
profils different vraiment. La comparaison repond a la question suivante :
« a competence egale sur le socle, lequel apporte quelque chose que les autres
n'ont pas ? »

Entierement deterministe : aucun appel modele. Chaque cellule reprend le
resultat du moteur de score, avec la methode de rapprochement qui l'a produite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.candidates.models import Candidate
from apps.jobs.models import JobOffer, JobSkill

from . import engine

# Au-dela, le tableau devient illisible et les colonnes trop etroites.
MAX_CANDIDATES = 4
# Ecart en deca duquel deux candidats sont juges a egalite sur un critere.
TIE_MARGIN = 0.05


@dataclass
class Cell:
    """Resultat d'un candidat sur une competence attendue."""

    score: float
    method: str
    matched_with: str | None
    best: bool = False

    @property
    def percent(self) -> int:
        return round(self.score * 100)

    @property
    def covered(self) -> bool:
        return self.score >= engine.GAP_THRESHOLD


@dataclass
class SkillRow:
    skill: str
    requirement: str
    weight: float
    cells: list[Cell] = field(default_factory=list)

    @property
    def is_required(self) -> bool:
        return self.requirement == JobSkill.Requirement.REQUIRED

    @property
    def discriminating(self) -> bool:
        """Vrai si les candidats ne se valent pas sur cette competence.

        C'est le seul interet du tableau : une ligne ou tout le monde est a
        100 % n'apprend rien et peut etre repliee.
        """
        scores = [cell.score for cell in self.cells]
        return bool(scores) and (max(scores) - min(scores)) > TIE_MARGIN


@dataclass
class CriterionRow:
    name: str
    label: str
    weight: float
    cells: list[Cell] = field(default_factory=list)


@dataclass
class Column:
    """Un candidat et son resultat d'ensemble."""

    candidate: Candidate
    overall: float
    gaps: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)

    @property
    def percent(self) -> int:
        return round(self.overall * 100)


@dataclass
class Comparison:
    offer: JobOffer
    columns: list[Column]
    skills: list[SkillRow]
    criteria: list[CriterionRow]
    engine_version: str
    semantic_used: bool

    @property
    def discriminating_skills(self) -> list[SkillRow]:
        return [row for row in self.skills if row.discriminating]

    @property
    def shared_skills(self) -> list[SkillRow]:
        return [row for row in self.skills if not row.discriminating]


def compare(offer: JobOffer, candidates: list[Candidate]) -> Comparison:
    """Compare des candidats sur une offre, competence par competence."""
    retenus = candidates[:MAX_CANDIDATES]
    resultats = [engine.score(candidate, offer) for candidate in retenus]

    colonnes = [
        Column(
            candidate=candidate,
            overall=resultat.overall,
            gaps=[gap["skill"] for gap in resultat.gaps],
        )
        for candidate, resultat in zip(retenus, resultats, strict=True)
    ]

    skills = _skill_rows(resultats)
    criteria = _criterion_rows(resultats)
    _mark_strengths(colonnes, skills)

    return Comparison(
        offer=offer,
        columns=colonnes,
        skills=skills,
        criteria=criteria,
        engine_version=resultats[0].engine_version if resultats else engine.ENGINE_VERSION,
        semantic_used=any(resultat.semantic_used for resultat in resultats),
    )


def _skill_rows(resultats: list[engine.ScoreResult]) -> list[SkillRow]:
    if not resultats:
        return []

    lignes: list[SkillRow] = []
    for index, reference in enumerate(resultats[0].skill_matches):
        cellules = [
            Cell(
                score=resultat.skill_matches[index].score,
                method=resultat.skill_matches[index].method,
                matched_with=resultat.skill_matches[index].matched_with,
            )
            for resultat in resultats
        ]
        _mark_best(cellules)
        lignes.append(
            SkillRow(
                skill=reference.required,
                requirement=reference.requirement,
                weight=reference.weight,
                cells=cellules,
            )
        )

    # Les competences obligatoires d'abord, puis les plus discriminantes.
    lignes.sort(key=lambda row: (not row.is_required, not row.discriminating, row.skill))
    return lignes


def _criterion_rows(resultats: list[engine.ScoreResult]) -> list[CriterionRow]:
    if not resultats:
        return []

    lignes: list[CriterionRow] = []
    for index, reference in enumerate(resultats[0].criteria):
        if not reference.applicable:
            continue
        cellules = [
            Cell(
                score=resultat.criteria[index].score,
                method="critere",
                matched_with=None,
            )
            for resultat in resultats
        ]
        _mark_best(cellules)
        lignes.append(
            CriterionRow(
                name=reference.name,
                label=reference.label,
                weight=reference.weight,
                cells=cellules,
            )
        )
    return lignes


def _mark_best(cellules: list[Cell]) -> None:
    """Marque le meilleur, sauf si tout le monde est a egalite.

    Souligner un « meilleur » quand l'ecart est negligeable donnerait a un
    hasard d'arrondi l'apparence d'un avantage.
    """
    if not cellules:
        return
    meilleur = max(cell.score for cell in cellules)
    pire = min(cell.score for cell in cellules)
    if meilleur - pire <= TIE_MARGIN or meilleur <= 0:
        return
    for cell in cellules:
        cell.best = cell.score >= meilleur - 1e-9


def _mark_strengths(colonnes: list[Column], skills: list[SkillRow]) -> None:
    """Releve, pour chaque candidat, ce qu'il est seul a apporter."""
    for position, colonne in enumerate(colonnes):
        colonne.strengths = [
            row.skill
            for row in skills
            if row.discriminating and row.cells[position].best
        ]
