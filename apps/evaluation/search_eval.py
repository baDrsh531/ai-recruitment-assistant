"""Harnais d'evaluation de la recherche plein texte.

Meme principe que le harnais de classement : les profils du jeu sont construits
en base, les requetes sont jouees, tout est annule ensuite. Ce qui change, c'est
la nature de la verite terrain — ici l'ensemble attendu est fixe *par
construction* : chaque profil est ecrit pour repondre ou non a des requetes
precises, et l'ensemble est arrete avant la premiere execution.

Les metriques retenues disent trois choses differentes :

* **rappel@5** — ai-je rate un profil pertinent ? C'est la question qui coute,
  pour la meme raison qu'ailleurs dans ce projet : un profil qu'on ne voit pas
  ne sera jamais rattrape.
* **MRR** — le premier bon resultat arrive-t-il assez haut ? Un recruteur ne
  descend pas la liste indefiniment.
* **precision@3** — le haut de liste est-il propre ?

La requete `sans_reponse` est traitee a part : sa reponse attendue est vide, et
une metrique de rappel n'a pas de sens sur un ensemble vide. Elle est verifiee
comme un booleen — la recherche a-t-elle su ne rien renvoyer.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from django.db import transaction

from apps.assistant import textsearch
from apps.candidates.models import (
    Candidate,
    CandidateSkill,
    Certification,
    Education,
    Experience,
)

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
KIND = "search"

# Seuils de non-regression. Comme ailleurs, les abaisser doit etre un acte
# conscient et non un ajustement pour faire passer la CI.
THRESHOLDS = {
    "recall_at_5": 0.90,
    "mrr": 0.80,
    "precision_at_3": 0.55,
}


@dataclass
class QueryResult:
    id: str
    query: str
    expected: list[str]
    returned: list[str]
    recall_at_5: float
    precision_at_3: float
    reciprocal_rank: float
    # Rappel@5 maximal atteignable : avec sept profils pertinents pour cinq
    # places, 0.71 est un sans-faute, pas un manque. Publier le plafond evite
    # de lire comme une lacune ce qui est une contrainte arithmetique.
    recall_at_5_ceiling: float = 1.0
    missed: list[str] = field(default_factory=list)

    @property
    def at_ceiling(self) -> bool:
        return abs(self.recall_at_5 - self.recall_at_5_ceiling) < 1e-9
    # Requete dont la reponse attendue est vide : mesuree comme un booleen.
    expects_nothing: bool = False
    answered_nothing: bool = False


@dataclass
class Report:
    dataset: str
    dataset_version: str
    semantic_used: bool
    hybrid: bool
    queries: list[QueryResult]
    aggregate: dict[str, float]

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "semantic_used": self.semantic_used,
            "hybrid": self.hybrid,
            "aggregate": self.aggregate,
            "queries": [asdict(query) for query in self.queries],
        }

    def failures(self) -> dict[str, tuple[float, float]]:
        return {
            name: (self.aggregate[name], seuil)
            for name, seuil in THRESHOLDS.items()
            if name in self.aggregate and self.aggregate[name] < seuil
        }


def load_dataset(name: str = "search_v1") -> dict:
    chemin = DATASETS_DIR / f"{name}.json"
    if not chemin.is_file():
        raise FileNotFoundError(f"Jeu d'evaluation introuvable : {name}")
    dataset = json.loads(chemin.read_text(encoding="utf-8"))
    if dataset.get("kind") != KIND:
        raise ValueError(
            f"« {name} » est un jeu de type « {dataset.get('kind') or 'inconnu'} », "
            f"attendu « {KIND} »."
        )
    return dataset


@contextmanager
def _corpus_temporaire(dataset: dict):
    """Cree les profils du jeu, puis annule tout."""
    with transaction.atomic():
        try:
            par_identifiant: dict[str, Candidate] = {}
            for spec in dataset["candidates"]:
                candidat = Candidate.objects.create(
                    full_name=spec["name"],
                    headline=spec.get("headline", ""),
                    email=f"{spec['id']}@evaluation.local",
                )
                for nom in spec.get("skills", []):
                    CandidateSkill.objects.create(candidate=candidat, name=nom)
                for experience in spec.get("experiences", []):
                    Experience.objects.create(
                        candidate=candidat,
                        title=experience.get("title", ""),
                        company=experience.get("company", ""),
                        description=experience.get("description", ""),
                    )
                for formation in spec.get("education", []):
                    Education.objects.create(
                        candidate=candidat,
                        degree=formation.get("degree", ""),
                        institution=formation.get("institution", ""),
                    )
                for certification in spec.get("certifications", []):
                    Certification.objects.create(candidate=candidat, name=certification)
                par_identifiant[spec["id"]] = candidat
            yield par_identifiant
        finally:
            transaction.set_rollback(True)


def _recall_at_k(retournes: list[str], attendus: set[str], k: int) -> float:
    if not attendus:
        return 1.0
    trouves = len(attendus & set(retournes[:k]))
    return trouves / len(attendus)


def _precision_at_k(retournes: list[str], attendus: set[str], k: int) -> float:
    tete = retournes[:k]
    if not tete:
        return 0.0
    return sum(1 for item in tete if item in attendus) / len(tete)


def _reciprocal_rank(retournes: list[str], attendus: set[str]) -> float:
    for position, item in enumerate(retournes, start=1):
        if item in attendus:
            return 1 / position
    return 0.0


def run(dataset_name: str = "search_v1", *, hybrid: bool = True) -> Report:
    dataset = load_dataset(dataset_name)
    resultats: list[QueryResult] = []
    semantique = False

    with _corpus_temporaire(dataset) as par_identifiant:
        inverse = {str(candidat.pk): cle for cle, candidat in par_identifiant.items()}

        for specification in dataset["queries"]:
            recherche = textsearch.search(
                specification["query"], limit=10, hybrid=hybrid
            )
            semantique = semantique or recherche.semantic_used

            retournes = [
                inverse[str(hit.candidate.pk)]
                for hit in recherche.hits
                if str(hit.candidate.pk) in inverse
            ]
            attendus = set(specification["relevant"])

            resultats.append(
                QueryResult(
                    id=specification["id"],
                    query=specification["query"],
                    expected=sorted(attendus),
                    returned=retournes,
                    recall_at_5=round(_recall_at_k(retournes, attendus, 5), 4),
                    precision_at_3=round(_precision_at_k(retournes, attendus, 3), 4),
                    reciprocal_rank=round(_reciprocal_rank(retournes, attendus), 4),
                    recall_at_5_ceiling=round(
                        min(1.0, 5 / len(attendus)) if attendus else 1.0, 4
                    ),
                    missed=sorted(attendus - set(retournes[:5])),
                    expects_nothing=not attendus,
                    answered_nothing=not retournes,
                )
            )

    # Les requetes sans reponse attendue sont exclues des moyennes : une
    # precision de 0 sur un ensemble vide punirait le bon comportement.
    mesurables = [item for item in resultats if not item.expects_nothing]
    aggregat: dict[str, float] = {}
    if mesurables:

        def moyenne(attribut: str) -> float:
            return round(
                sum(getattr(item, attribut) for item in mesurables) / len(mesurables), 4
            )

        aggregat["recall_at_5"] = moyenne("recall_at_5")
        # Ce que le rappel@5 peut valoir au mieux sur ce jeu. L'ecart avec la
        # ligne precedente est le seul manque reel ; le reste tient a ce qu'une
        # requete peut compter plus de profils pertinents que de places.
        aggregat["recall_at_5_ceiling"] = moyenne("recall_at_5_ceiling")
        aggregat["mrr"] = moyenne("reciprocal_rank")
        aggregat["precision_at_3"] = moyenne("precision_at_3")
    aggregat["empty_queries_handled"] = round(
        sum(
            1 for item in resultats if item.expects_nothing and item.answered_nothing
        ) / max(1, sum(1 for item in resultats if item.expects_nothing)),
        4,
    )

    return Report(
        dataset=dataset["name"],
        dataset_version=dataset.get("version", "0"),
        semantic_used=semantique,
        hybrid=hybrid,
        queries=resultats,
        aggregate=aggregat,
    )
