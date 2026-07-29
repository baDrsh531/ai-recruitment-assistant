"""Schema des filtres extraits d'une question en langage naturel.

Le modele ne choisit pas de candidats : il traduit la question en criteres
structures. Le filtrage est ensuite fait par du code, sur la base. C'est ce qui
garantit qu'aucun candidat ne peut etre invente, et que la meme question posee
deux fois renvoie la meme liste.
"""

from __future__ import annotations

FILTER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "skills_all": {
            "type": "array",
            "description": (
                "Competences que le candidat doit TOUTES posseder. Intitules "
                "seuls, sans verbe : « Python », « Django »."
            ),
            "items": {"type": "string"},
        },
        "skills_any": {
            "type": "array",
            "description": "Competences dont au moins une suffit.",
            "items": {"type": "string"},
        },
        "skills_none": {
            "type": "array",
            "description": (
                "Competences que le candidat ne doit PAS avoir. Utilise pour "
                "« qui maitrise Django mais pas React »."
            ),
            "items": {"type": "string"},
        },
        "languages": {
            "type": "array",
            "description": "Langues que le candidat doit parler.",
            "items": {"type": "string"},
        },
        "min_years": {
            "type": "number",
            "description": "Anciennete totale minimale en annees. 0 si non precisee.",
        },
        "min_education": {
            "type": "integer",
            "description": (
                "Niveau d'etudes minimal, en annees apres le baccalaureat : "
                "0 aucun, 3 licence, 5 master, 8 doctorat."
            ),
        },
        "min_score": {
            "type": "number",
            "description": (
                "Score de compatibilite minimal, entre 0 et 1. 0 si la question "
                "ne parle pas de score."
            ),
        },
        "location": {
            "type": "string",
            "description": "Ville ou region mentionnee. Chaine vide sinon.",
        },
        "order_by_score": {
            "type": "boolean",
            "description": (
                "Vrai si la question demande les meilleurs, un classement, ou "
                "le candidat le plus adapte."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                "Nombre de candidats demandes. 0 si la question n'en fixe pas."
            ),
        },
        "rejected_criteria": {
            "type": "array",
            "description": (
                "Criteres de la question qui ont ete IGNORES parce que "
                "discriminatoires : age, genre, origine, nationalite, religion, "
                "situation familiale, handicap, apparence. Vide sinon."
            ),
            "items": {"type": "string"},
        },
    },
}
