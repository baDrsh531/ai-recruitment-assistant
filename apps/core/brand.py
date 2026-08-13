"""L'identite visuelle, en un seul endroit.

Une marque appliquee a la main dans chaque gabarit derive : l'ecran dit une
chose, le PDF une autre, le courriel une troisieme. Ce module tient les
couleurs, le nom et la marque, et les trois sorties y puisent.

**La source est le SVG**, `static/img/mark.svg`. Les PNG dont le courriel et le
PDF ont besoin en sont rendus a la demande, puis gardes en memoire. Stocker des
PNG a cote du SVG aurait cree autant d'occasions de les laisser diverger.

Pourquoi un PNG pour le courriel : Gmail et Outlook **retirent les SVG**, et
une image chargee depuis une adresse distante est bloquee par defaut ou trahit
l'ouverture du message. La marque part donc en piece jointe liee (`cid:`), qui
s'affiche sans requete sortante.
"""

from __future__ import annotations

import functools
import pathlib

from django.conf import settings

# --- Les couleurs, reprises des jetons de `static/css/app.css` ---------------
ENCRE = "#131922"
ENCRE_CLAIRE = "#ffffff"
BRAND = "#4f46e5"
BRAND_TEXTE = "#3730a3"
TEXTE = "#10151c"
TEXTE_ATTENUE = "#56606e"
BORDURE = "#e0e6ee"
FOND = "#f5f7fa"
SURFACE = "#ffffff"

NOM = "Recrutement.IA"
# Le point separe deux mots que la marque distingue par la couleur.
NOM_RACINE, NOM_SUFFIXE = "Recrutement", ".IA"

# Identifiant de la piece jointe liee, cote courriel.
CID_MARQUE = "marque-recrutement-ia"

_SVG = pathlib.Path(settings.BASE_DIR) / "static" / "img" / "mark.svg"


@functools.lru_cache(maxsize=1)
def marque_svg() -> str:
    """Contenu brut du SVG, lu une fois."""
    return _SVG.read_text(encoding="utf-8")


def marque_svg_colore(encre: str = ENCRE) -> str:
    """SVG avec une encre fixe, pour les sorties qui ignorent `currentColor`."""
    return marque_svg().replace("currentColor", encre)


@functools.lru_cache(maxsize=8)
def marque_png(taille: int = 96, encre: str = ENCRE) -> bytes:
    """Marque rendue en PNG transparent, de `taille` pixels de cote.

    Rendue depuis le SVG plutot que lue depuis un fichier : le jour ou la
    marque change, il n'y a qu'un fichier a modifier, et rien a regenerer a la
    main. Le cache evite de refaire le rendu a chaque courriel.
    """
    import fitz

    document = fitz.open("svg", marque_svg_colore(encre).encode())
    page = document[0]
    facteur = taille / page.rect.width
    pixmap = page.get_pixmap(matrix=fitz.Matrix(facteur, facteur), alpha=True)
    return pixmap.tobytes("png")


def organisation() -> str:
    """Nom qui signe les documents et les messages."""
    return getattr(settings, "OUTREACH_ORGANISATION", "") or NOM
