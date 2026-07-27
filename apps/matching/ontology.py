"""Ontologie de competences.

Le rapprochement par mots-cles est inutilisable en recrutement : « DRF »,
« Django REST Framework » et « django-rest-framework » designent la meme
chose, et un candidat qui maitrise Django maitrise necessairement Python.
Cette ontologie encode ces deux relations :

  - ALIASES : formes ecrites differentes d'une meme competence ;
  - IMPLIES : une competence en implique une autre (relation dirigee) ;
  - RELATED : competences voisines, avec un degre de transferabilite.

C'est un noyau de depart, volontairement restreint au domaine technique et
maintenu a la main. La cible est la taxonomie ESCO (officielle, multilingue,
gratuite) : `load_esco()` documente le point d'insertion. En attendant, ce
noyau couvre les cas les plus frequents et reste inspectable — ce qui vaut
mieux, pour un systeme qui trie des candidatures, qu'une correspondance
purement statistique.
"""

from __future__ import annotations

import functools

# --- Formes ecrites equivalentes -------------------------------------------
ALIASES: dict[str, str] = {
    "drf": "django rest framework",
    "django-rest-framework": "django rest framework",
    "django rest": "django rest framework",
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "ts": "typescript",
    "node": "node.js",
    "nodejs": "node.js",
    "postgres": "postgresql",
    "psql": "postgresql",
    "pgsql": "postgresql",
    "k8s": "kubernetes",
    "gcp": "google cloud",
    "aws cloud": "aws",
    "amazon web services": "aws",
    "ms sql": "sql server",
    "mssql": "sql server",
    "sklearn": "scikit-learn",
    "scikit learn": "scikit-learn",
    "tf": "tensorflow",
    "llms": "llm",
    "large language models": "llm",
    "modeles de langage": "llm",
    "grands modeles de langage": "llm",
    "retrieval augmented generation": "rag",
    "ci/cd": "ci-cd",
    "cicd": "ci-cd",
    "integration continue": "ci-cd",
    "html5": "html",
    "css3": "css",
    "reactjs": "react",
    "react.js": "react",
    "vuejs": "vue",
    "vue.js": "vue",
    "restful": "rest",
    "api rest": "rest",
    "apis rest": "rest",
    "gestion de version": "git",
    "anglais professionnel": "anglais",
}

# --- Implications : maitriser la cle implique maitriser les valeurs --------
IMPLIES: dict[str, tuple[str, ...]] = {
    "django": ("python",),
    "django rest framework": ("django", "python", "rest"),
    "flask": ("python",),
    "fastapi": ("python", "rest"),
    "celery": ("python",),
    "pandas": ("python",),
    "numpy": ("python",),
    "scikit-learn": ("python", "machine learning"),
    "pytorch": ("python", "deep learning", "machine learning"),
    "tensorflow": ("python", "deep learning", "machine learning"),
    "deep learning": ("machine learning",),
    "llm": ("machine learning", "nlp"),
    "rag": ("llm", "nlp"),
    "transformers": ("deep learning", "nlp", "python"),
    "spring boot": ("java", "rest"),
    "spring": ("java",),
    "react": ("javascript",),
    "vue": ("javascript",),
    "angular": ("typescript", "javascript"),
    "next.js": ("react", "javascript"),
    "typescript": ("javascript",),
    "node.js": ("javascript",),
    "kubernetes": ("docker", "linux"),
    "terraform": ("infrastructure as code",),
    "ansible": ("infrastructure as code", "linux"),
    "postgresql": ("sql",),
    "mysql": ("sql",),
    "sql server": ("sql",),
    "oracle": ("sql",),
    "sqlite": ("sql",),
    "airflow": ("python", "etl"),
    "dbt": ("sql", "etl"),
    "spark": ("big data",),
    "hadoop": ("big data",),
    "elasticsearch": ("recherche",),
    "graphql": ("api",),
    "rest": ("api",),
    "ci-cd": ("devops",),
    "jenkins": ("ci-cd", "devops"),
    "github actions": ("ci-cd", "devops"),
    "gitlab ci": ("ci-cd", "devops"),
    "docker": ("linux",),
    "bash": ("linux",),
    "aws": ("cloud",),
    "azure": ("cloud",),
    "google cloud": ("cloud",),
}

# --- Competences voisines : transferabilite partielle, relation symetrique --
RELATED: dict[frozenset[str], float] = {
    frozenset({"pytorch", "tensorflow"}): 0.75,
    frozenset({"react", "vue"}): 0.65,
    frozenset({"react", "angular"}): 0.60,
    frozenset({"vue", "angular"}): 0.60,
    frozenset({"postgresql", "mysql"}): 0.80,
    frozenset({"postgresql", "sql server"}): 0.70,
    frozenset({"aws", "azure"}): 0.65,
    frozenset({"aws", "google cloud"}): 0.65,
    frozenset({"azure", "google cloud"}): 0.65,
    frozenset({"django", "flask"}): 0.70,
    frozenset({"django", "fastapi"}): 0.70,
    frozenset({"flask", "fastapi"}): 0.80,
    frozenset({"java", "c#"}): 0.60,
    frozenset({"jenkins", "github actions"}): 0.75,
    frozenset({"jenkins", "gitlab ci"}): 0.75,
    frozenset({"github actions", "gitlab ci"}): 0.85,
    frozenset({"terraform", "ansible"}): 0.60,
    frozenset({"spark", "hadoop"}): 0.70,
    frozenset({"mongodb", "postgresql"}): 0.45,
    frozenset({"redis", "memcached"}): 0.80,
}

# La competence du candidat implique celle demandee : il sait faire.
# (offre : Python — candidat : Django, qui presuppose Python)
IMPLICATION_SCORE = 0.85
# Sens inverse : le candidat n'a que le prerequis de ce qui est demande.
# (offre : Django — candidat : Python) Utile, mais loin d'etre equivalent :
# savoir Python ne veut pas dire savoir Django. Credit partiel seulement.
PREREQUISITE_SCORE = 0.45


def normalize(name: str) -> str:
    """Forme canonique d'un intitule de competence."""
    cleaned = " ".join((name or "").strip().lower().replace("_", " ").split())
    return ALIASES.get(cleaned, cleaned)


@functools.lru_cache(maxsize=512)
def closure(skill: str) -> frozenset[str]:
    """Ensemble des competences impliquees, transitivement, par `skill`.

    django -> {django, python} ; drf -> {django rest framework, django, python, rest, api}
    """
    canonical = normalize(skill)
    seen: set[str] = set()
    stack = [canonical]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(IMPLIES.get(current, ()))
    return frozenset(seen)


def relatedness(required: str, held: str) -> float:
    """Dans quelle mesure `held` (candidat) couvre `required` (offre). Dans [0, 1].

    **La relation est dirigee, et ce n'est pas un detail.** Un candidat qui
    maitrise Django maitrise necessairement Python : il couvre une exigence
    « Python ». L'inverse est faux — savoir Python ne signifie pas savoir
    Django. Traiter ces deux cas de la meme facon crediterait des competences
    que le candidat n'a pas, ce qui est inacceptable pour un tri de
    candidatures.

    1.0    identiques (alias compris) ;
    0.85   la competence du candidat implique celle demandee ;
    0.45   le candidat n'a que le prerequis de la competence demandee ;
    0.45-0.85 voisines declarees (relation symetrique, elle) ;
    0.0    aucune relation connue — aux embeddings de trancher.
    """
    a, b = normalize(required), normalize(held)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in closure(b):
        return IMPLICATION_SCORE
    if b in closure(a):
        return PREREQUISITE_SCORE
    return RELATED.get(frozenset({a, b}), 0.0)


def load_esco(path: str) -> int:  # pragma: no cover - point d'extension
    """Point d'insertion pour la taxonomie ESCO.

    ESCO fournit ~13 000 competences avec libelles multilingues et relations
    hierarchiques. Le chargeur devra alimenter ALIASES depuis les libelles
    alternatifs et IMPLIES depuis la hierarchie « broader/narrower ».
    """
    raise NotImplementedError(
        "Chargement ESCO non implemente. Telecharger le jeu de donnees sur "
        "https://esco.ec.europa.eu/ et alimenter ALIASES / IMPLIES."
    )
