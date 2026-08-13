"""Faire annoter le jeu par quelqu'un d'autre, et mesurer l'accord.

La limite la plus serieuse des jeux annotes de ce projet n'est pas leur taille,
c'est qu'**une seule personne les a ecrits**. Le projet mesure par ailleurs un
kappa de Cohen de 0,25 entre deux evaluateurs sur des dossiers reels : rien ne
dit que ces annotations-ci echapperaient a cet ecart.

Ce module ne fait pas apparaitre un second annotateur. Il enleve la seule chose
qui l'empechait d'exister : jusqu'ici, meme un recruteur volontaire n'aurait rien
eu a annoter — les cas vivent dans un JSON de deux mille lignes, melant l'offre,
les candidats et les notes deja posees.

Deux operations, et rien de plus :

- **exporter** les cas sous une forme qu'on remplit sans lire une ligne de code,
  avec la colonne de pertinence vide ;
- **comparer** deux jeux d'annotations et en tirer un kappa.

L'export ne montre jamais les notes existantes. Les montrer transformerait le
second annotateur en relecteur du premier, et l'accord mesure ne vaudrait plus
rien : on ne mesure pas un accord en soufflant la reponse.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Colonnes de l'export. `pertinence` est la seule a remplir.
COLONNES = [
    "cas", "candidat", "identifiant", "offre", "anciennete", "diplome",
    "competences", "langues", "pertinence",
]

NIVEAUX = {
    3: "tout a fait adapte — a recevoir sans hesiter",
    2: "adapte — merite un entretien",
    1: "peu adapte — a regarder faute de mieux",
    0: "hors sujet",
}


def kappa_categoriel(gauche: list[int], droite: list[int]) -> float | None:
    """Kappa de Cohen sur des categories, ici quatre niveaux de pertinence.

    **Distinct de `agreement.cohen_kappa`, qui est binaire** — retenu ou ecarte.
    Appliquer la version binaire a quatre niveaux donnerait un chiffre faux sans
    rien signaler : elle traite `2` et `3` comme deux valeurs vraies, donc comme
    un accord.

    Renvoie `None` quand le kappa est indefini : c'est le cas si un annotateur a
    donne la meme note partout, l'accord attendu au hasard valant alors 1. Rendre
    0.0 se lirait « aucun accord », ce qui serait faux, et rendre 1.0 flatterait.
    """
    if not gauche or len(gauche) != len(droite):
        return None

    total = len(gauche)
    observe = sum(1 for a, b in zip(gauche, droite, strict=True) if a == b) / total

    parts_gauche = Counter(gauche)
    parts_droite = Counter(droite)
    hasard = sum(
        (parts_gauche[niveau] / total) * (parts_droite[niveau] / total)
        for niveau in set(parts_gauche) | set(parts_droite)
    )

    if hasard >= 1.0:
        return None
    return (observe - hasard) / (1 - hasard)


def exporter(dataset: dict) -> str:
    """Rend les cas sous forme de CSV, colonne de pertinence vide.

    Le fichier se remplit dans un tableur, sans rien connaitre du projet. Une
    ligne par candidat, avec de quoi juger : ce que l'offre demande, et ce que
    le profil apporte.
    """
    tampon = io.StringIO()
    graveur = csv.DictWriter(tampon, fieldnames=COLONNES, lineterminator="\n")
    graveur.writeheader()

    for cas in dataset["cases"]:
        offre = cas["offer"]
        exigences = ", ".join(
            f"{item['name']}"
            + (f" ({item['min_years']} ans)" if item.get("min_years") else "")
            for item in offre.get("required_skills", [])
        )
        resume_offre = f"{offre['title']} — exige : {exigences or 'rien de precis'}"

        for candidat in cas["candidates"]:
            graveur.writerow({
                "cas": cas["id"],
                "candidat": candidat.get("name", candidat["id"]),
                "identifiant": candidat["id"],
                "offre": resume_offre,
                "anciennete": candidat.get("years", 0),
                "diplome": candidat.get("education", 0),
                "competences": ", ".join(
                    f"{item['name']} ({item.get('years', 0):.0f} ans,"
                    f" vu en {item.get('last_used_year', '?')})"
                    for item in candidat.get("skills", [])
                ) or "aucune",
                "langues": ", ".join(
                    f"{item['language']} {item['level']}"
                    for item in candidat.get("languages", [])
                ) or "aucune",
                # Volontairement vide : montrer la note existante ferait du
                # second annotateur un relecteur du premier.
                "pertinence": "",
            })
    return tampon.getvalue()


def _lire_annotations(contenu: str) -> dict[str, int]:
    """Lit un CSV rempli. Cle = « cas/identifiant », valeur = pertinence."""
    annotations: dict[str, int] = {}
    for ligne in csv.DictReader(io.StringIO(contenu)):
        valeur = (ligne.get("pertinence") or "").strip()
        if not valeur:
            continue
        try:
            note = int(valeur)
        except ValueError as erreur:
            raise ValueError(
                f"Pertinence illisible pour {ligne.get('identifiant')} : "
                f"« {valeur} ». Attendu un entier de 0 a 3."
            ) from erreur
        if note not in NIVEAUX:
            raise ValueError(
                f"Pertinence hors bornes pour {ligne.get('identifiant')} : "
                f"{note}. Attendu 0, 1, 2 ou 3."
            )
        annotations[f"{ligne['cas']}/{ligne['identifiant']}"] = note
    return annotations


def annotations_du_jeu(dataset: dict) -> dict[str, int]:
    """Les notes deja posees, sous la meme forme que celles d'un CSV."""
    return {
        f"{cas['id']}/{candidat['id']}": candidat["relevance"]
        for cas in dataset["cases"]
        for candidat in cas["candidates"]
    }


@dataclass
class Accord:
    """Ce que deux annotateurs ont en commun."""

    communs: int = 0
    identiques: int = 0
    kappa: float | None = None
    manquants: list[str] = field(default_factory=list)
    inconnus: list[str] = field(default_factory=list)
    ecarts: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def accord_brut(self) -> float:
        return self.identiques / self.communs if self.communs else 0.0

    @property
    def suffisant(self) -> bool:
        """Assez de profils communs pour qu'un kappa veuille dire quelque chose.

        Le seuil est arbitraire et assume. Ce qui ne l'est pas : refuser de
        publier un kappa calcule sur cinq profils, qui bougerait de 0,2 a 0,8
        selon un seul desaccord.
        """
        return self.communs >= 30

    @property
    def lecture(self) -> str:
        if not self.communs:
            return (
                "Aucun profil annote par les deux. Le second fichier est vide, "
                "ou ses identifiants ne correspondent pas a ceux du jeu."
            )
        if not self.suffisant:
            return (
                f"{self.communs} profils annotes par les deux : trop peu pour "
                f"qu'un kappa signifie quelque chose. Accord brut "
                f"{self.accord_brut:.0%}, publie sans kappa."
            )
        if self.kappa is None:
            return (
                f"Accord brut {self.accord_brut:.0%} sur {self.communs} profils. "
                f"Kappa indefini : l'un des deux annotateurs a donne la meme "
                f"note partout, il n'y a aucune variance a correler."
            )
        return (
            f"Kappa de Cohen {self.kappa:.2f} sur {self.communs} profils "
            f"(accord brut {self.accord_brut:.0%}). Le kappa retire l'accord "
            f"du au hasard : c'est lui qu'il faut lire, pas le pourcentage."
        )


def comparer(dataset: dict, contenu_csv: str) -> Accord:
    """Confronte les annotations du jeu a celles d'un second annotateur."""
    references = annotations_du_jeu(dataset)
    secondes = _lire_annotations(contenu_csv)

    accord = Accord()
    accord.inconnus = sorted(set(secondes) - set(references))
    accord.manquants = sorted(set(references) - set(secondes))

    communs = sorted(set(references) & set(secondes))
    gauche = [references[cle] for cle in communs]
    droite = [secondes[cle] for cle in communs]

    accord.communs = len(communs)
    accord.identiques = sum(1 for a, b in zip(gauche, droite, strict=True) if a == b)
    accord.ecarts = [
        (cle, a, b)
        for cle, a, b in zip(communs, gauche, droite, strict=True)
        if a != b
    ]
    if communs:
        accord.kappa = kappa_categoriel(gauche, droite)
    return accord


def charger(chemin: Path) -> dict:
    return json.loads(Path(chemin).read_text(encoding="utf-8"))
