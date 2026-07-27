"""Ancrage des preuves : retrouver dans le document la citation produite par le modele.

Le modele doit citer un extrait verbatim pour chaque donnee extraite. Ce module
verifie que cette citation existe reellement dans le CV et calcule sa position
exacte (page, offsets, rectangle englobant). Une citation introuvable fait
tomber la donnee en `verified=False` — c'est le garde-fou contre l'hallucination.

La comparaison est tolerante : le modele recopie rarement au caractere pres
(accents, espaces insecables, ligatures, majuscules). On normalise donc les
deux cotes tout en conservant une table de correspondance vers les offsets
d'origine, indispensable pour retrouver les mots et donc les coordonnees.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

from .extractors import ExtractedDocument, Page

# En dessous de ce taux de recouvrement, la citation est consideree inventee.
MIN_MATCH_RATIO = 0.60
# En dessous, une citation ne dit plus rien : deux ou trois caracteres se
# retrouvent dans n'importe quel document.
MIN_QUOTE_LENGTH = 3
# En dessous, la citation est exacte mais peu discriminante — typiquement le
# seul intitule de la competence, « Python ». Le modele l'a bien lue dans le
# document, mais elle ne prouve pas grand-chose : on l'accepte en exigeant une
# correspondance sur mot entier, avec une confiance reduite.
#
# Confondre ce cas avec une citation inventee etait un defaut reel : sur un CV
# de test, trois competences pourtant presentes remontaient « non etayees ».
SHORT_QUOTE_LENGTH = 12
SHORT_QUOTE_RATIO = 0.75


@dataclass(slots=True)
class ResolvedEvidence:
    page: int
    text: str
    char_start: int
    char_end: int
    bbox: list[float] | None
    ratio: float

    @property
    def verified(self) -> bool:
        return self.ratio >= MIN_MATCH_RATIO


def _normalize(text: str) -> tuple[str, list[int]]:
    """Normalise et renvoie la table offset_normalise -> offset_origine.

    Suppression des accents, mise en minuscules, espaces compresses. La table
    permet de revenir a la position exacte dans le texte d'origine apres une
    recherche effectuee sur la forme normalisee.
    """
    output: list[str] = []
    mapping: list[int] = []
    previous_is_space = True

    for index, char in enumerate(text):
        if char.isspace():
            if not previous_is_space:
                output.append(" ")
                mapping.append(index)
                previous_is_space = True
            continue

        decomposed = unicodedata.normalize("NFKD", char)
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
        if not stripped:
            continue
        for c in stripped:
            output.append(c)
            mapping.append(index)
        previous_is_space = False

    return "".join(output), mapping


class EvidenceResolver:
    """Retrouve une citation dans un document deja extrait."""

    def __init__(self, document: ExtractedDocument) -> None:
        self._pages = document.pages
        self._normalized = [_normalize(page.text) for page in document.pages]

    def resolve(self, quote: str) -> ResolvedEvidence | None:
        quote = (quote or "").strip()
        if len(quote) < MIN_QUOTE_LENGTH:
            return None

        needle, _ = _normalize(quote)
        if not needle:
            return None

        best: ResolvedEvidence | None = None
        for page, (haystack, mapping) in zip(self._pages, self._normalized, strict=True):
            candidate = self._match_in_page(page, haystack, mapping, needle)
            if candidate and (best is None or candidate.ratio > best.ratio):
                best = candidate
                if best.ratio >= 0.999:
                    break
        return best

    # ----------------------------------------------------------------------
    def _match_in_page(
        self, page: Page, haystack: str, mapping: list[int], needle: str
    ) -> ResolvedEvidence | None:
        if not haystack:
            return None

        if len(needle) < SHORT_QUOTE_LENGTH:
            # Citation breve : exigence renforcee, correspondance sur mot
            # entier, pour qu'un « SQL » ne se valide pas sur « MySQL ».
            match = re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack)
            if match is None:
                return None
            return self._build(
                page, mapping, match.start(), match.end(), SHORT_QUOTE_RATIO
            )

        position = haystack.find(needle)
        if position >= 0:
            return self._build(page, mapping, position, position + len(needle), 1.0)

        # Repli : plus longue sous-chaine commune. Couvre les citations
        # tronquees ou legerement reformulees par le modele.
        matcher = difflib.SequenceMatcher(None, haystack, needle, autojunk=False)
        block = matcher.find_longest_match(0, len(haystack), 0, len(needle))
        if block.size == 0:
            return None

        ratio = block.size / len(needle)
        if ratio < MIN_MATCH_RATIO:
            return None
        return self._build(page, mapping, block.a, block.a + block.size, ratio)

    def _build(
        self, page: Page, mapping: list[int], start: int, end: int, ratio: float
    ) -> ResolvedEvidence:
        char_start = mapping[start]
        char_end = mapping[min(end, len(mapping)) - 1] + 1
        return ResolvedEvidence(
            page=page.number,
            text=page.text[char_start:char_end],
            char_start=char_start,
            char_end=char_end,
            bbox=_bbox_for_range(page, char_start, char_end),
            ratio=round(ratio, 3),
        )


def _bbox_for_range(page: Page, start: int, end: int) -> list[float] | None:
    """Rectangle englobant les mots couverts par l'intervalle de caracteres."""
    covered = [
        word.bbox
        for word in page.words
        if word.char_start < end and word.char_end > start
    ]
    if not covered:
        return None
    return [
        min(box[0] for box in covered),
        min(box[1] for box in covered),
        max(box[2] for box in covered),
        max(box[3] for box in covered),
    ]
