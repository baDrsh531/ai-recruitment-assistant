"""Diagnostic de la qualite d'extraction.

C'est ce module qui decide si le texte natif suffit ou s'il faut passer par le
modele vision. Sans lui, un CV scanne produirait une extraction vide et un CV
sur deux colonnes produirait des lignes entrelacees — les deux echecs les plus
frequents des parsers de CV.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .extractors import ExtractedDocument, Page

# En dessous de ce nombre de caracteres par page, le PDF est presume scanne.
MIN_CHARS_PER_PAGE = 180
# Part minimale de mots situes dans une colonne pour la considerer reelle.
MIN_COLUMN_SHARE = 0.15
# Largeur minimale du couloir vide separant deux colonnes, en part de page.
MIN_GUTTER_SHARE = 0.06


@dataclass(slots=True)
class QualityReport:
    char_count: int
    chars_per_page: float
    looks_scanned: bool
    multi_column_pages: list[int]
    has_text_layer: bool

    @property
    def is_multi_column(self) -> bool:
        return bool(self.multi_column_pages)

    @property
    def needs_vision(self) -> bool:
        """Vrai si le texte natif ne peut pas etre exploite tel quel."""
        return self.looks_scanned or self.is_multi_column

    def as_dict(self) -> dict:
        data = asdict(self)
        data["is_multi_column"] = self.is_multi_column
        data["needs_vision"] = self.needs_vision
        return data


def detect_multi_column(page: Page) -> bool:
    """Detecte une mise en page multi-colonnes par recherche de couloir vide.

    On projette les mots sur l'axe horizontal et on cherche une bande verticale
    sans aucun mot, assez large et assez centrale, avec du contenu significatif
    de part et d'autre. Une bande vide sur un bord n'est qu'une marge.
    """
    if page.width <= 0 or len(page.words) < 40:
        return False

    bins = 50
    occupancy = [0] * bins
    for word in page.words:
        start = max(0, min(bins - 1, int(word.bbox[0] / page.width * bins)))
        end = max(0, min(bins - 1, int(word.bbox[2] / page.width * bins)))
        for index in range(start, end + 1):
            occupancy[index] += 1

    minimum_gutter = max(2, int(MIN_GUTTER_SHARE * bins))
    total_words = len(page.words)

    run_start = None
    for index in range(bins):
        if occupancy[index] == 0:
            if run_start is None:
                run_start = index
            continue

        if run_start is not None:
            run_length = index - run_start
            # Une marge touche un bord ; un couloir inter-colonnes est interieur.
            is_interior = run_start > 0 and index < bins
            if run_length >= minimum_gutter and is_interior:
                left = sum(occupancy[:run_start])
                right = sum(occupancy[index:])
                if (
                    left >= MIN_COLUMN_SHARE * total_words
                    and right >= MIN_COLUMN_SHARE * total_words
                ):
                    return True
            run_start = None

    return False


def assess(document: ExtractedDocument) -> QualityReport:
    page_count = max(1, document.page_count)
    char_count = document.char_count
    chars_per_page = char_count / page_count

    multi_column = [
        page.number for page in document.pages if detect_multi_column(page)
    ]

    return QualityReport(
        char_count=char_count,
        chars_per_page=round(chars_per_page, 1),
        looks_scanned=chars_per_page < MIN_CHARS_PER_PAGE,
        multi_column_pages=multi_column,
        has_text_layer=char_count > 0,
    )
