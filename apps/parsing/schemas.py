"""Schema JSON de sortie de l'extraction.

Ce schema est envoye au serveur d'inference en decodage contraint : le modele
ne peut pas produire autre chose. Il n'y a donc aucun parsing de texte libre
cote Python, et donc aucune des pannes qui vont avec.

Chaque element porte un champ `evidence` : la citation verbatim qui le
justifie. `apps/parsing/evidence.py` la retrouve ensuite dans le document.
"""

from __future__ import annotations

_EVIDENCE = {
    "type": "string",
    "description": (
        "Extrait recopie mot pour mot du document, entre 10 et 120 caracteres, "
        "qui justifie cet element. Chaine vide si aucune citation possible."
    ),
}


def _evidenced(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": {**properties, "evidence": _EVIDENCE},
    }


CV_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "identity": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "linkedin": {"type": "string"},
                "github": {"type": "string"},
                "location": {"type": "string"},
                "headline": {
                    "type": "string",
                    "description": "Titre professionnel, ex. 'Ingenieur backend Python'.",
                },
            },
        },
        "skills": {
            "type": "array",
            "description": (
                "Competences techniques et outils uniquement, avec l'intitule "
                "tel qu'ecrit dans le CV. N'y mets jamais une langue parlee — "
                "elle a son propre champ — ni un titre de section comme "
                "« COMPETENCES » ou « LANGUES »."
            ),
            "items": _evidenced(
                {
                    "name": {"type": "string"},
                    "years": {
                        "type": "number",
                        "description": "Annees de pratique si le CV le precise, sinon 0.",
                    },
                    "last_used_year": {
                        "type": "integer",
                        "description": "Derniere annee d'utilisation, 0 si inconnue.",
                    },
                }
            ),
        },
        "experiences": {
            "type": "array",
            "items": _evidenced(
                {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "start_date": {
                        "type": "string",
                        "description": "Format AAAA-MM. Chaine vide si absent.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Format AAAA-MM. Chaine vide si poste en cours.",
                    },
                    "description": {"type": "string"},
                }
            ),
        },
        "education": {
            "type": "array",
            "items": _evidenced(
                {
                    "degree": {"type": "string"},
                    "field_of_study": {"type": "string"},
                    "institution": {"type": "string"},
                    "level": {
                        "type": "integer",
                        "description": (
                            "Nombre d'annees apres le baccalaureat : 0 aucun, 1 bac, "
                            "3 licence, 5 master, 8 doctorat."
                        ),
                    },
                    "graduation_year": {"type": "integer"},
                }
            ),
        },
        "languages": {
            "type": "array",
            "items": _evidenced(
                {
                    "language": {"type": "string"},
                    "level": {
                        "type": "string",
                        "enum": ["A1", "A2", "B1", "B2", "C1", "C2", "NAT"],
                        "description": "Niveau CECRL. 'NAT' pour langue maternelle.",
                    },
                }
            ),
        },
        "certifications": {
            "type": "array",
            "items": _evidenced(
                {
                    "name": {"type": "string"},
                    "issuer": {"type": "string"},
                    "obtained_year": {"type": "integer"},
                }
            ),
        },
    },
}
