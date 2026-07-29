"""Application des filtres, par du code et sur la base.

Le modele traduit la question en criteres ; c'est ici qu'ils sont appliques.
La separation est le coeur de la fonctionnalite : un modele qui choisirait
lui-meme les candidats pourrait en inventer, en oublier, ou changer d'avis
d'une execution a l'autre. Ici, la meme question donne toujours la meme liste,
et chaque candidat retourne existe reellement.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from apps.candidates.models import Application
from apps.jobs.models import JobOffer
from apps.matching.models import MatchScore
from apps.matching.ontology import relatedness

# Un candidat « possede » une competence demandee si l'ontologie les relie
# au moins a ce degre — « DRF » satisfait une demande de Django.
SKILL_MATCH_FLOOR = 0.80
DEFAULT_LIMIT = 10
MAX_LIMIT = 25

# Le modele range parfois une langue parlee parmi les competences : « quels
# candidats parlent francais et anglais ? » devenait une recherche de
# competences nommees « Francais » et « Anglais », et ne renvoyait personne.
# Le prompt le precise desormais, mais la correction est faite ici aussi : une
# consigne de prompt n'est pas une garantie.
LANGUES_CONNUES = {
    "francais", "anglais", "arabe", "espagnol", "allemand", "italien",
    "portugais", "neerlandais", "russe", "chinois", "mandarin", "japonais",
    "berbere", "amazigh", "turc", "polonais", "roumain", "french", "english",
    "arabic", "spanish", "german", "italian", "portuguese", "dutch",
}


@dataclass
class FilterSet:
    skills_all: list[str] = field(default_factory=list)
    skills_any: list[str] = field(default_factory=list)
    skills_none: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    min_years: float = 0.0
    min_education: int = 0
    min_score: float = 0.0
    location: str = ""
    order_by_score: bool = False
    limit: int = 0
    rejected_criteria: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict) -> FilterSet:
        """Construit un jeu de filtres depuis la sortie du modele, en la bornant."""

        def liste(cle: str) -> list[str]:
            valeurs = payload.get(cle) or []
            return [str(v).strip() for v in valeurs if str(v).strip()][:12]

        # Une langue rangee parmi les competences ne trouverait jamais
        # personne : on la remet a sa place avant de filtrer.
        competences_toutes, langues_egarees = _separer_langues(liste("skills_all"))
        competences_une, autres_egarees = _separer_langues(liste("skills_any"))
        langues = liste("languages") + langues_egarees + autres_egarees

        return cls(  # noqa: RUF100
            skills_all=competences_toutes,
            skills_any=competences_une,
            skills_none=liste("skills_none"),
            languages=list(dict.fromkeys(langues))[:12],
            min_years=max(0.0, float(payload.get("min_years") or 0)),
            min_education=max(0, int(payload.get("min_education") or 0)),
            min_score=min(1.0, max(0.0, float(payload.get("min_score") or 0))),
            location=str(payload.get("location") or "").strip()[:80],
            order_by_score=bool(payload.get("order_by_score")),
            limit=min(MAX_LIMIT, max(0, int(payload.get("limit") or 0))),
            rejected_criteria=liste("rejected_criteria"),
        )

    @property
    def is_empty(self) -> bool:
        return not any(
            [
                self.skills_all, self.skills_any, self.skills_none, self.languages,
                self.min_years, self.min_education, self.min_score, self.location,
            ]
        )

    def as_dict(self) -> dict:
        return {
            "skills_all": self.skills_all,
            "skills_any": self.skills_any,
            "skills_none": self.skills_none,
            "languages": self.languages,
            "min_years": self.min_years,
            "min_education": self.min_education,
            "min_score": self.min_score,
            "location": self.location,
            "order_by_score": self.order_by_score,
            "limit": self.limit,
            "rejected_criteria": self.rejected_criteria,
        }

    def summary(self) -> list[str]:
        """Description lisible des criteres retenus, pour affichage."""
        morceaux = []
        if self.skills_all:
            morceaux.append("toutes ces competences : " + ", ".join(self.skills_all))
        if self.skills_any:
            morceaux.append("au moins une parmi : " + ", ".join(self.skills_any))
        if self.skills_none:
            morceaux.append("sans : " + ", ".join(self.skills_none))
        if self.languages:
            morceaux.append("parle : " + ", ".join(self.languages))
        if self.min_years:
            morceaux.append(f"au moins {self.min_years:g} an(s) d'experience")
        if self.min_education:
            morceaux.append(f"niveau bac+{self.min_education} minimum")
        if self.min_score:
            morceaux.append(f"score d'au moins {self.min_score * 100:.0f} %")
        if self.location:
            morceaux.append(f"situe a {self.location}")
        return morceaux


@dataclass
class Match:
    """Un candidat retenu, avec ce qui a permis de le retenir."""

    application: Application
    score: float
    matched_skills: list[str] = field(default_factory=list)

    @property
    def candidate(self):
        return self.application.candidate

    @property
    def percent(self) -> int:
        return round(self.score * 100)


def apply(offer: JobOffer, filters: FilterSet) -> list[Match]:
    """Renvoie les candidatures de l'offre satisfaisant tous les criteres."""
    candidatures = (
        offer.applications.select_related("candidate")
        .prefetch_related("candidate__skills", "candidate__languages")
        .exclude(stage=Application.Stage.WITHDRAWN)
    )

    derniers: dict[str, float] = {}
    for score in MatchScore.objects.filter(application__offer=offer).order_by(
        "application_id", "-created_at"
    ):
        derniers.setdefault(str(score.application_id), score.effective_score)

    retenus: list[Match] = []
    for application in candidatures:
        candidate = application.candidate
        competences = [skill.name for skill in candidate.skills.all()]

        trouvees = [
            demandee
            for demandee in filters.skills_all
            if _possede(competences, demandee)
        ]
        if len(trouvees) < len(filters.skills_all):
            continue

        if filters.skills_any:
            parmi = [d for d in filters.skills_any if _possede(competences, d)]
            if not parmi:
                continue
            trouvees += parmi

        if any(_possede(competences, exclue) for exclue in filters.skills_none):
            continue

        if filters.languages:
            parlees = {_sans_accents(item.language) for item in candidate.languages.all()}
            if not all(_sans_accents(langue) in parlees for langue in filters.languages):
                continue

        if candidate.total_experience_years < filters.min_years:
            continue
        if candidate.highest_education < filters.min_education:
            continue

        if filters.location and not _meme_lieu(candidate.location, filters.location):
            continue

        score = derniers.get(str(application.pk), 0.0)
        if score < filters.min_score:
            continue

        retenus.append(
            Match(application=application, score=score, matched_skills=trouvees)
        )

    retenus.sort(key=lambda item: item.score, reverse=True)
    limite = filters.limit or DEFAULT_LIMIT
    return retenus[:limite]


# --- Comparaisons -----------------------------------------------------------
def _separer_langues(valeurs: list[str]) -> tuple[list[str], list[str]]:
    """Separe les competences des langues parlees qui s'y seraient glissees."""
    competences, langues = [], []
    for valeur in valeurs:
        (langues if _sans_accents(valeur) in LANGUES_CONNUES else competences).append(valeur)
    return competences, langues


def _possede(competences: list[str], demandee: str) -> bool:
    """Vrai si l'une des competences du candidat couvre celle demandee.

    Le rapprochement passe par l'ontologie, dans le sens candidat -> demande :
    « DRF » satisfait « Django », l'inverse serait faux.
    """
    return any(relatedness(demandee, detenue) >= SKILL_MATCH_FLOOR for detenue in competences)


def _sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFKD", (texte or "").strip().lower())
    return "".join(char for char in decompose if not unicodedata.combining(char))


def _meme_lieu(candidat: str, demande: str) -> bool:
    gauche, droite = _sans_accents(candidat), _sans_accents(demande)
    return bool(gauche) and (droite in gauche or gauche in droite)
