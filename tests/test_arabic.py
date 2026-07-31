"""Tests du traitement de l'arabe.

Le point de depart est une mesure : sur un CV arabe genere, l'extraction brute
retrouve 2 champs sur 8 ; avec normalisation, 7 sur 8. Sans cette etape, un CV
arabe est un document illisible pour le systeme — pas partiellement lisible,
illisible : les points de code extraits n'egalent aucune saisie au clavier.
"""

from __future__ import annotations

import pytest

from apps.assistant.textsearch import tokenise
from apps.candidates.duplicates import cle_nom
from apps.core import arabic
from apps.evaluation import cv_factory
from apps.parsing.extractors import extract

# Formes de presentation telles qu'un PDF les stocke, et leur equivalent saisi.
NOM_PDF = "ﺱﺍﺭﺓ"
NOM_SAISI = "سارة"

PROFIL = {
    "full_name": "سارة العمراني",
    "headline": "مهندسة بيانات",
    "email": "sara.elamrani@example.com",
    "phone": "+212 661 22 33 44",
    "location": "الدار البيضاء",
    "skills": ["Python", "SQL", "Airflow", "تحليل البيانات"],
    "languages": [{"language": "العربية", "level": "NAT"}],
    "experiences": [
        {
            "title": "مهندسة بيانات",
            "company": "المجموعة البنكية",
            "start": "2021-03",
            "end": "2024-06",
            "description": "تطوير أنظمة كشف الاحتيال على المعاملات",
        }
    ],
    "education": [
        {
            "degree": "ماجستير في المعلوماتية",
            "institution": "جامعة الحسن الثاني",
            "year": 2020,
        }
    ],
}


# --- Detection ---------------------------------------------------------------
def test_arabic_is_detected():
    assert arabic.contient_de_l_arabe("سارة")
    assert arabic.contient_de_l_arabe("Ingenieur سارة")
    assert not arabic.contient_de_l_arabe("Ingenieur backend")
    assert not arabic.contient_de_l_arabe("")


def test_the_dominant_script_is_measured():
    """Un CV marocain melange couramment les deux ecritures."""
    assert arabic.proportion_arabe("سارة العمراني") == 1.0
    assert arabic.proportion_arabe("Sara El Amrani") == 0.0
    assert 0.3 < arabic.proportion_arabe("سارة Sara") < 0.7
    assert arabic.proportion_arabe("+212 661 22") == 0.0


# --- Normalisation -----------------------------------------------------------
def test_presentation_forms_become_base_letters():
    """C'est l'etape sans laquelle aucune egalite de chaine n'est possible.

    On compare les deux formes normalisees entre elles, et non la forme du PDF
    a la saisie brute : la normalisation replie aussi le ta marbuta, si bien
    que la forme canonique n'est ni l'une ni l'autre.
    """
    assert NOM_PDF != NOM_SAISI, "les deux graphies different bien"
    assert arabic.normaliser(NOM_PDF) == arabic.normaliser(NOM_SAISI)


@pytest.mark.parametrize(
    ("variante", "attendu"),
    [
        ("أحمد", "احمد"),
        ("إبراهيم", "ابراهيم"),
        ("آمنة", "امنه"),
        ("سارة", "ساره"),
        ("مصطفى", "مصطفي"),
    ],
)
def test_graphic_variants_of_the_same_name_converge(variante, attendu):
    assert arabic.normaliser(variante) == attendu


def test_tatweel_is_removed():
    """L'allongement typographique etire un mot sans rien y ajouter."""
    assert arabic.normaliser("سـارة") == arabic.normaliser("سارة")


def test_diacritics_are_removed():
    """Rarement saisis dans une requete, parfois presents dans un CV soigne."""
    assert arabic.normaliser("سَارَة") == arabic.normaliser("سارة")


def test_arabic_indic_digits_become_latin():
    assert arabic.normaliser("٢٠٢٤") == "2024"
    assert arabic.normaliser("۲۰۲۴") == "2024"


def test_latin_text_is_untouched():
    """La fonction doit pouvoir etre appelee sans condition."""
    for texte in ["Ingenieur backend", "C++ / Python 3.11", "", "  "]:
        assert arabic.normaliser(texte) == texte


def test_mixed_text_keeps_its_latin_part():
    resultat = arabic.normaliser("سارة — Data Engineer")
    assert "Data Engineer" in resultat


# --- Ordre visuel ------------------------------------------------------------
def test_a_fully_arabic_line_in_visual_order_is_reversed():
    visuel = arabic.normaliser("ﻱﻥﺍﺭﻡﻉﻝﺍ ﺓﺭﺍﺱ")
    assert arabic.en_ordre_logique(visuel) == arabic.normaliser("سارة العمراني")


def test_a_mixed_line_is_left_alone():
    """Inverser casserait la partie latine, et rien ne permet de reconstituer
    l'ordre sans l'algorithme bidirectionnel complet."""
    ligne = "سارة sara@example.com"
    assert arabic.en_ordre_logique(ligne) == ligne


def test_a_latin_line_is_left_alone():
    assert arabic.en_ordre_logique("Data Engineer") == "Data Engineer"


# --- La mesure qui justifie le module ----------------------------------------
@pytest.fixture(scope="module")
def cv_arabe():
    if cv_factory.police_arabe() is None:
        pytest.skip("aucune police contenant les glyphes arabes sur ce systeme")
    return extract(cv_factory.build(PROFIL, layout="arabe"), "cv.pdf").full_text


def test_the_generated_cv_really_contains_arabic(cv_arabe):
    assert arabic.contient_de_l_arabe(cv_arabe)
    assert arabic.proportion_arabe(cv_arabe) > 0.5


def test_raw_extraction_finds_almost_nothing(cv_arabe):
    """Sans normalisation, seuls les champs latins ressortent."""
    arabes = [PROFIL["full_name"], PROFIL["headline"], "المجموعة البنكية"]
    assert not any(valeur in cv_arabe for valeur in arabes)
    assert PROFIL["email"] in cv_arabe


def test_normalisation_makes_the_arabic_fields_readable(cv_arabe):
    normalise = arabic.normaliser(cv_arabe)
    attendus = [
        PROFIL["full_name"],
        PROFIL["headline"],
        "تحليل البيانات",
        "المجموعة البنكية",
        "ماجستير في المعلوماتية",
    ]
    manques = [
        valeur for valeur in attendus if arabic.normaliser(valeur) not in normalise
    ]
    assert manques == [], f"champs perdus : {manques}"


def test_the_latin_fields_survive_normalisation(cv_arabe):
    normalise = arabic.normaliser(cv_arabe)
    assert PROFIL["email"] in normalise
    assert "Airflow" in normalise


def test_a_standalone_hamza_is_lost_by_the_renderer(cv_arabe):
    """Limite connue, et c'est le generateur qui la porte, pas la chaine.

    « الدار البيضاء » ressort en « البيضا » : le hamza final (U+0621) n'est pas
    rendu dans le PDF genere. La normalisation n'y peut rien — le caractere
    n'est pas dans le document. Les vrais PDF arabes presentent la meme classe
    de perte, ce qui rend la mesure representative plutot qu'artificiellement
    propre ; le chiffre de 7 champs sur 8 est donc un plancher.
    """
    normalise = arabic.normaliser(cv_arabe)
    assert "الدار" in normalise
    assert "البيضاء" not in normalise
    assert "البيضا" in normalise


# --- Recherche ---------------------------------------------------------------
def test_an_arabic_query_is_tokenised():
    jetons = tokenise("تحليل البيانات")
    assert "تحليل" in jetons
    assert "البيانات" in jetons


def test_a_query_typed_by_hand_matches_a_pdf(cv_arabe):
    """Le resultat pratique : chercher « تحليل » trouve le CV."""
    jetons_document = set(tokenise(cv_arabe))
    for requete in ["تحليل البيانات", "الاحتيال", "Airflow"]:
        trouves = [jeton for jeton in tokenise(requete) if jeton in jetons_document]
        assert trouves, f"« {requete} » ne retrouve rien"


def test_tokenising_keeps_latin_and_arabic_side_by_side():
    jetons = tokenise("سارة Data Engineer")
    assert arabic.normaliser("سارة") in jetons
    assert "data" in jetons


def test_graphic_variants_are_searchable_alike():
    assert tokenise("أحمد") == tokenise("احمد")


# --- Doublons ----------------------------------------------------------------
def test_two_spellings_of_an_arabic_name_share_a_key():
    assert cle_nom("سارة العمراني") == cle_nom("ساره العمراني")


def test_the_name_key_ignores_word_order_in_arabic():
    assert cle_nom("سارة العمراني") == cle_nom("العمراني سارة")


def test_an_arabic_name_from_a_pdf_matches_a_typed_one():
    """Sans normalisation, ces deux graphies n'auraient jamais ete rapprochees."""
    assert cle_nom(NOM_PDF) == cle_nom(NOM_SAISI)


def test_different_arabic_names_keep_different_keys():
    assert cle_nom("سارة العمراني") != cle_nom("أحمد العمراني")


def test_a_latin_name_key_is_unchanged():
    assert cle_nom("Badr Sahraoui") == cle_nom("SAHRAOUI Badr")


# --- Fabrique ----------------------------------------------------------------
def test_the_arabic_layout_is_offered():
    assert "arabe" in cv_factory.LAYOUTS


def test_an_unknown_layout_is_refused():
    with pytest.raises(ValueError, match="Mise en page inconnue"):
        cv_factory.build(PROFIL, layout="cuneiforme")
