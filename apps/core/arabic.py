"""Normalisation de l'arabe pour la comparaison.

Un CV arabe extrait d'un PDF ne rend pas les lettres qu'on croit. Le PDF stocke
des **formes de presentation** — les variantes contextuelles d'une lettre selon
sa position dans le mot — dans la plage U+FE70–U+FEFF. « سارة » ecrit dans le
document ressort en « ﺱﺍﺭﺓ » : d'autres points de code, donc aucune egalite de
chaine, donc aucune correspondance possible avec une requete tapee au clavier.

Sans cette normalisation, chercher « سارة » ne trouve jamais « سارة ». Ce n'est
pas un cas limite : c'est le comportement par defaut de tout PDF arabe.

**Pourquoi ici et pas a l'extraction.** La normalisation change la longueur du
texte — la ligature lam-alef (U+FEFB) est un caractere qui en devient deux.
Appliquee au moment de l'extraction, elle decalerait tous les offsets et
casserait la correspondance entre un extrait et sa bbox, sur laquelle repose
tout l'ancrage des preuves du projet. Elle se fait donc a la comparaison, comme
le retrait des accents.

Ce que la normalisation fait, et pourquoi chaque etape :

  formes de presentation -> lettres de base   sinon aucune egalite de chaine
  alif a hamza -> alif nu                     أحمد et احمد designent la meme personne
  ta marbuta -> ha                            سارة et ساره sont deux graphies courantes
  alif maqsura -> ya                          مصطفى et مصطفي, meme mot
  tatweel retire                              allongement typographique, pas une lettre
  diacritiques retires                        rarement saisis, jamais dans une requete
  chiffres arabo-indiens -> chiffres latins   ٢٠٢٤ et 2024 sont la meme annee
"""

from __future__ import annotations

import re
import unicodedata

# Plages du bloc arabe. Sert a decider si un texte merite ce traitement.
_ARABE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

# Allongement typographique : etire visuellement un mot sans rien y ajouter.
TATWEEL = "ـ"

# Diacritiques (harakat). Presents dans un texte soigne, absents d'une requete.
_DIACRITIQUES = re.compile(r"[ً-ٰٟۖ-ۭ]")

# Variantes graphiques qui designent la meme lettre a la lecture.
_EQUIVALENCES = {
    "آ": "ا",  # alif madda      آ -> ا
    "أ": "ا",  # alif hamza haut أ -> ا
    "إ": "ا",  # alif hamza bas  إ -> ا
    "ٱ": "ا",  # alif wasla      ٱ -> ا
    "ة": "ه",  # ta marbuta      ة -> ه
    "ى": "ي",  # alif maqsura    ى -> ي
    "ؤ": "و",  # waw hamza       ؤ -> و
    "ئ": "ي",  # ya hamza        ئ -> ي
}

# Chiffres arabo-indiens, orientaux et persans.
_CHIFFRES = {
    **{chr(0x0660 + i): str(i) for i in range(10)},
    **{chr(0x06F0 + i): str(i) for i in range(10)},
}

_TABLE = str.maketrans({**_EQUIVALENCES, **_CHIFFRES, TATWEEL: ""})


def contient_de_l_arabe(texte: str) -> bool:
    """Le texte comporte-t-il au moins un caractere arabe ?"""
    return bool(_ARABE.search(texte or ""))


def proportion_arabe(texte: str) -> float:
    """Part de caracteres arabes parmi les lettres.

    Sert a decider de la langue dominante d'un document : un CV marocain melange
    couramment l'arabe, le francais et des noms de technologies en anglais.
    """
    lettres = [caractere for caractere in (texte or "") if caractere.isalpha()]
    if not lettres:
        return 0.0
    return sum(1 for caractere in lettres if _ARABE.match(caractere)) / len(lettres)


def normaliser(texte: str) -> str:
    """Ramene un texte arabe a une forme comparable.

    Sans effet sur un texte qui ne contient pas d'arabe : la fonction peut donc
    etre appelee sans condition dans une chaine de comparaison.
    """
    if not texte:
        return ""
    if not contient_de_l_arabe(texte):
        return texte

    # NFKC convertit les formes de presentation en lettres de base et decompose
    # les ligatures. C'est l'etape qui rend l'egalite de chaine possible.
    normalise = unicodedata.normalize("NFKC", texte)
    normalise = _DIACRITIQUES.sub("", normalise)
    return normalise.translate(_TABLE)


def en_ordre_logique(texte: str) -> str:
    """Remet en ordre logique un texte extrait en ordre visuel.

    Certains producteurs de PDF ecrivent l'arabe dans l'ordre ou il s'affiche —
    de droite a gauche — plutot que dans l'ordre ou il se lit. Le texte extrait
    est alors a l'envers, et aucune normalisation ne le rattrape.

    L'inversion n'est appliquee que si la ligne est **entierement** arabe :
    sur une ligne melangee, inverser casserait la partie latine, et il n'y a pas
    de moyen fiable de reconstituer l'ordre d'origine sans l'algorithme
    bidirectionnel complet. Le cas melange est donc laisse tel quel, et c'est
    une limite assumee plutot qu'une correction hasardeuse.
    """
    lignes = []
    for ligne in (texte or "").splitlines():
        nettoyee = ligne.strip()
        if nettoyee and proportion_arabe(nettoyee) >= 0.999:
            lignes.append(ligne[::-1])
        else:
            lignes.append(ligne)
    return "\n".join(lignes)


def cle_de_comparaison(texte: str) -> str:
    """Forme retenue pour comparer deux chaines pouvant contenir de l'arabe."""
    return normaliser(texte or "").strip().lower()
