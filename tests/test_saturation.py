"""Saturation du score, et provenance des annotations.

Deux mesures qui disent la meme chose sous deux angles : un chiffre n'a de sens
qu'accompagne de ce qui le limite.

La saturation compte les profils que le moteur ne peut plus departager parce
qu'ils touchent le plafond. Elle ne corrige rien : etaler le haut de l'echelle
est un arbitrage produit.

La provenance dit qui a annote. Une verite terrain est l'opinion de qui l'a
ecrite, et un jeu qui ne le dit pas se presente comme un fait.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from django.conf import settings
from django.core.management import call_command

from apps.evaluation import saturation
from apps.matching import engine

DATASETS = pathlib.Path(settings.BASE_DIR) / "apps" / "evaluation" / "datasets"


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


# --- Ce que la saturation compte ---------------------------------------------
def _cas(pertinences, candidats=None):
    return saturation.CasSature(
        cas="essai",
        candidats=candidats or len(pertinences),
        au_plafond=len(pertinences),
        identifiants=[f"p{index}" for index in range(len(pertinences))],
        pertinences=list(pertinences),
    )


def test_equal_profiles_at_the_ceiling_cost_nothing():
    """Deux profils que l'annotation tient pour equivalents peuvent etre a
    egalite : le moteur ne confond rien."""
    cas = _cas([3, 3])

    assert not cas.pertinences_confondues


def test_the_ceiling_that_hides_a_real_difference_is_the_one_that_counts():
    """C'est la seule forme de saturation qui coute quelque chose : le
    recruteur separe ces profils, le moteur non."""
    cas = _cas([3, 2])

    assert cas.pertinences_confondues
    assert cas.genant


def test_a_large_tie_is_flagged_even_without_confusion():
    """Trois profils indifferencies en tete restent un bloc que le recruteur
    doit trancher lui-meme, meme s'ils sont vraiment equivalents."""
    cas = _cas([3, 3, 3])

    assert not cas.pertinences_confondues
    assert cas.genant, "au-dela de deux, l'egalite se voit a l'ecran"


def test_a_single_profile_at_the_ceiling_is_not_a_saturation(db):
    """Le meilleur profil a 1,000 ne confond personne : c'est le resultat
    attendu, pas un defaut."""
    mesure = saturation.mesurer()

    for item in mesure.satures:
        assert item.au_plafond >= 2, f"{item.cas} : un seul profil n'est pas une egalite"


def test_the_ceiling_tolerates_the_float_noise():
    """Le moteur additionne des flottants : deux profils egaux peuvent differer
    sur le dernier bit, et un seuil a 1.0 exact les separerait a tort."""
    assert saturation.PLAFOND < 1.0
    assert saturation.PLAFOND > 0.999


# --- Ce que le jeu annote revele ---------------------------------------------
def test_the_engine_really_saturates_on_the_annotated_set(db):
    """Le constat qui justifie ce module. Chiffres relevés, pas supposés."""
    mesure = saturation.mesurer()

    assert mesure.candidats_total > 100
    assert mesure.au_plafond > 0, "aucun plafond : le module n'aurait plus d'objet"
    assert mesure.confusions, (
        "aucune confusion mesuree — si le moteur a change, mettre a jour le "
        "README qui annonce le contraire"
    )


def test_the_confusion_appears_on_small_pools_too(db):
    """Le defaut ne demande pas un grand vivier.

    Le README a d'abord affirme qu'il ne se voyait pas sur des cas a cinq
    candidats. La mesure dit le contraire : il etait deja la, il n'y etait pas
    cherche.
    """
    mesure = saturation.mesurer()

    assert mesure.effectif_declencheur is not None
    assert mesure.effectif_declencheur <= 5


def test_the_reading_names_the_confusions(db):
    mesure = saturation.mesurer()

    assert "plafond" in mesure.lecture
    if mesure.confusions:
        assert "confondent" in mesure.lecture


def test_the_command_runs(db):
    call_command("measure_saturation")


# --- Provenance des annotations ----------------------------------------------
@pytest.mark.parametrize(
    "fichier", ["ranking_v1.json", "extraction_v1.json", "search_v1.json"]
)
def test_every_dataset_says_who_annotated_it(fichier):
    """Une verite terrain est l'opinion de qui l'a ecrite. Un jeu qui ne le dit
    pas se presente comme un fait."""
    donnees = json.loads((DATASETS / fichier).read_text(encoding="utf-8"))
    provenance = donnees.get("provenance")

    assert provenance, f"{fichier} : provenance absente"
    assert provenance["annotateurs"] >= 1
    assert provenance["annote_par"]
    assert provenance["note"]


@pytest.mark.parametrize(
    "fichier", ["ranking_v1.json", "extraction_v1.json", "search_v1.json"]
)
def test_a_single_annotator_is_stated_as_a_limit(fichier):
    """Tant qu'une seule personne annote, aucun accord inter-annotateur n'existe
    — et le jeu doit le dire plutot que de laisser croire a un consensus."""
    donnees = json.loads((DATASETS / fichier).read_text(encoding="utf-8"))
    provenance = donnees["provenance"]

    if provenance["annotateurs"] == 1:
        assert provenance["accord_inter_annotateur"] is None
        assert "une seule personne" in provenance["note"]
    else:
        assert provenance["accord_inter_annotateur"] is not None, (
            "plusieurs annotateurs declares : leur accord doit etre mesure"
        )
