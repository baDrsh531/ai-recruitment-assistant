"""Construction des jeux de donnees destines aux graphiques.

Les vues preparent des dictionnaires serialisables ; le rendu est fait dans le
navigateur en SVG, sans aucune bibliotheque externe. Le gabarit emet toujours
un tableau HTML equivalent : sans JavaScript, la page reste lisible et chaque
valeur reste accessible.

Regles de forme retenues :
  - une seule serie -> une seule couleur, jamais un degrade par valeur ;
  - barres horizontales pour les categories a intitules longs ;
  - courbe reservee au temps ;
  - deux series au maximum, avec legende obligatoire.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Limite de lisibilite : au-dela, les categories de queue sont regroupees.
DEFAULT_TOP = 8


@dataclass
class Series:
    name: str
    # Emplacement categoriel, 1 ou 2. Suit l'entite, jamais son rang.
    slot: int = 1

    def as_dict(self) -> dict:
        return {"name": self.name, "slot": self.slot}


@dataclass
class Chart:
    id: str
    title: str
    kind: str = "bar"  # bar | stack | line
    subtitle: str = ""
    unit: str = ""
    note: str = ""
    series: list[Series] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.rows or not any(sum(row["values"]) for row in self.rows)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "unit": self.unit,
            "series": [item.as_dict() for item in self.series],
            "rows": self.rows,
        }


def bar(
    chart_id: str,
    title: str,
    pairs,
    *,
    subtitle: str = "",
    unit: str = "",
    note: str = "",
    series_name: str = "",
    top: int | None = DEFAULT_TOP,
    other_label: str = "autres",
) -> Chart:
    """Barres horizontales a serie unique.

    Au-dela de `top` categories, la queue est repliee dans « Autres » plutot
    que d'ajouter des barres illisibles. Le repli est visible, jamais silencieux.
    """
    entries = [(str(label), float(value)) for label, value in pairs]
    entries.sort(key=lambda item: item[1], reverse=True)

    if top is not None and len(entries) > top:
        tete, queue = entries[: top - 1], entries[top - 1 :]
        # « 15 autres competences » et non « Autres (15) » : la barre porte la
        # somme des effectifs, le libelle le nombre de categories repliees.
        # Ecrits de la meme facon, les deux nombres se confondent a la lecture.
        entries = tete + [
            (f"{len(queue)} {other_label.lower()}", sum(v for _, v in queue))
        ]

    return Chart(
        id=chart_id,
        title=title,
        kind="bar",
        subtitle=subtitle,
        unit=unit,
        note=note,
        series=[Series(series_name or title, slot=1)],
        rows=[{"label": label, "values": [value]} for label, value in entries],
    )


def ordered_bar(
    chart_id: str,
    title: str,
    pairs,
    *,
    subtitle: str = "",
    unit: str = "",
    note: str = "",
    series_name: str = "",
) -> Chart:
    """Barres horizontales dont l'ordre des categories porte du sens.

    Tranches d'anciennete, paliers de score : l'ordre est celui fourni, il n'est
    pas retrie par valeur.
    """
    return Chart(
        id=chart_id,
        title=title,
        kind="bar",
        subtitle=subtitle,
        unit=unit,
        note=note,
        series=[Series(series_name or title, slot=1)],
        rows=[{"label": str(label), "values": [float(value)]} for label, value in pairs],
    )


def grouped_bar(
    chart_id: str,
    title: str,
    rows,
    names: tuple[str, str],
    *,
    subtitle: str = "",
    unit: str = "",
    note: str = "",
    kind: str = "bar",
) -> Chart:
    """Deux series comparees categorie par categorie. Legende obligatoire."""
    return Chart(
        id=chart_id,
        title=title,
        kind=kind,
        subtitle=subtitle,
        unit=unit,
        note=note,
        series=[Series(names[0], slot=1), Series(names[1], slot=2)],
        rows=[
            {"label": str(label), "values": [float(first), float(second)]}
            for label, first, second in rows
        ],
    )


def line(
    chart_id: str,
    title: str,
    pairs,
    *,
    subtitle: str = "",
    unit: str = "",
    note: str = "",
    series_name: str = "",
) -> Chart:
    """Serie temporelle. L'ordre chronologique est celui fourni."""
    return Chart(
        id=chart_id,
        title=title,
        kind="line",
        subtitle=subtitle,
        unit=unit,
        note=note,
        series=[Series(series_name or title, slot=1)],
        rows=[{"label": str(label), "values": [float(value)]} for label, value in pairs],
    )


def percentile(values: list[float], fraction: float) -> float:
    """Percentile par interpolation lineaire. Renvoie 0 sur une liste vide."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    bas = int(position)
    haut = min(bas + 1, len(ordered) - 1)
    poids = position - bas
    return ordered[bas] * (1 - poids) + ordered[haut] * poids
