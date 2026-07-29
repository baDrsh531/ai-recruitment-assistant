"""Tests de la calibration du seuil de shortlist.

Le seuil est un jugement traduit en chiffre. Ces tests verifient que le chiffre
est bien celui que le jugement implique — notamment que le F-beta penche du bon
cote, et que la marge autour du seuil retenu est publiee plutot que passee sous
silence.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.evaluation import threshold
from apps.matching import engine


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture(autouse=True)
def vider_le_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


# --- Calcul d'un point -------------------------------------------------------
def test_a_threshold_of_zero_keeps_everyone():
    observations = [(0.9, True), (0.5, True), (0.1, False)]
    point = threshold._point(0.0, observations)

    assert point.retained == 3
    assert point.false_negative == 0
    assert point.recall == 1.0


def test_a_threshold_of_one_keeps_almost_no_one():
    observations = [(0.9, True), (0.5, True), (0.1, False)]
    point = threshold._point(1.0, observations)

    assert point.retained == 0
    assert point.false_negative == 2
    assert point.recall == 0.0
    assert point.precision == 0.0


def test_the_counts_add_up():
    observations = [(0.9, True), (0.7, False), (0.5, True), (0.1, False)]
    point = threshold._point(0.6, observations)

    assert point.true_positive == 1
    assert point.false_positive == 1
    assert point.false_negative == 1
    assert point.retained == point.true_positive + point.false_positive


def test_recall_weighs_more_than_precision():
    """beta = 2 : manquer un bon profil doit couter plus que recevoir un moyen."""
    # Rappel parfait, precision mediocre.
    genereux = threshold._point(0.0, [(0.9, True), (0.5, False), (0.4, False)])
    # Precision parfaite, rappel mediocre.
    severe = threshold._point(0.95, [(0.99, True), (0.9, True), (0.8, True)])

    assert genereux.recall == 1.0
    assert severe.precision == 1.0
    assert genereux.f_beta > severe.f_beta, (
        "avec beta=2, mieux vaut recevoir trop que manquer quelqu'un"
    )


def test_a_missed_good_profile_is_surfaced():
    point = threshold._point(0.8, [(0.9, True), (0.5, True)])
    assert point.missed == 1


# --- Calibration complete ----------------------------------------------------
def test_the_calibration_runs_on_the_annotated_dataset(db):
    calibration = threshold.calibrate()

    assert calibration.dataset == "ranking_v1"
    assert calibration.total_candidates > 0
    assert calibration.total_relevant > 0
    assert calibration.total_relevant < calibration.total_candidates
    assert calibration.recommended is not None


def test_the_curve_covers_every_threshold(db):
    calibration = threshold.calibrate()

    assert len(calibration.curve) == 101
    assert calibration.curve[0].threshold == 0.0
    assert calibration.curve[-1].threshold == 1.0


def test_retention_only_decreases_as_the_threshold_rises(db):
    calibration = threshold.calibrate()
    retenus = [point.retained for point in calibration.curve]

    assert retenus == sorted(retenus, reverse=True)


def test_missed_profiles_only_increase(db):
    calibration = threshold.calibrate()
    manques = [point.false_negative for point in calibration.curve]

    assert manques == sorted(manques)


def test_the_recommended_threshold_maximises_the_f_beta(db):
    calibration = threshold.calibrate()
    meilleur = max(point.f_beta for point in calibration.curve)

    assert calibration.recommended.f_beta == meilleur


def test_the_recommended_threshold_sits_in_the_middle_of_its_plateau(db):
    """Une borne du plateau serait fragile : au bord haut, un point de score
    perdu fait perdre un bon profil."""
    calibration = threshold.calibrate()

    assert calibration.plateau_low <= calibration.recommended.threshold
    assert calibration.recommended.threshold <= calibration.plateau_high
    milieu = (calibration.plateau_low + calibration.plateau_high) / 2
    assert abs(calibration.recommended.threshold - milieu) <= threshold.STEP


def test_the_margin_is_published(db):
    """Un seuil sans sa marge se lit comme une certitude qu'il n'est pas."""
    calibration = threshold.calibrate()

    assert calibration.plateau_width_points >= 0
    assert calibration.plateau_high >= calibration.plateau_low


def test_the_calibration_serialises(db):
    donnees = threshold.calibrate().as_dict()

    assert set(donnees) >= {
        "recommended", "curve", "plateau", "perfectly_separable",
        "total_candidates", "total_relevant", "beta",
    }


def test_the_calibration_leaves_no_trace(db):
    """Le harnais construit des offres et des candidats : tout doit disparaitre."""
    from apps.candidates.models import Candidate
    from apps.jobs.models import JobOffer

    avant = (Candidate.objects.count(), JobOffer.objects.count())
    threshold.calibrate()
    assert (Candidate.objects.count(), JobOffer.objects.count()) == avant


def test_the_sampled_curve_keeps_the_recommended_point(db):
    calibration = threshold.calibrate()
    echantillon = threshold.sampled_curve(calibration, step=0.10)

    assert calibration.recommended in echantillon
    seuils = [point.threshold for point in echantillon]
    assert seuils == sorted(seuils)


def test_the_cached_calibration_is_not_recomputed(db, monkeypatch):
    """Le balayage represente un scoring complet du jeu annote : une page de
    travail ne doit pas le refaire a chaque affichage."""
    from django.core.cache import cache

    appels = []
    vrai_calibrate = threshold.calibrate

    def compter(nom="ranking_v1"):
        appels.append(nom)
        return vrai_calibrate(nom)

    monkeypatch.setattr(threshold, "calibrate", compter)

    premier = threshold.cached()
    second = threshold.cached()

    assert len(appels) == 1, "le second appel doit venir du cache"
    assert cache.get(f"{threshold.CACHE_KEY}:ranking_v1") is not None
    # Le cache serialise : l'objet est equivalent, pas identique.
    assert second.recommended.threshold == premier.recommended.threshold
    assert second.total_candidates == premier.total_candidates


# --- Integration a l'interface -----------------------------------------------
def test_the_threshold_page_renders(client, db, recruteur):
    client.force_login(recruteur)
    reponse = client.get(reverse("evaluation:threshold"))

    assert reponse.status_code == 200
    assert reponse.context["calibration"].recommended is not None
    assert reponse.context["curve"]


def test_the_page_states_what_the_threshold_does_not_do(client, db, recruteur):
    client.force_login(recruteur)
    contenu = client.get(reverse("evaluation:threshold")).content.decode()

    assert "n&#x27;ecarte aucune candidature" in contenu or "ecarte aucune" in contenu


def test_the_ranking_marks_the_cut(client, db, recruteur):
    from apps.candidates.models import Application, Candidate, CandidateSkill
    from apps.jobs.models import JobOffer, JobSkill
    from apps.matching.services import score_application

    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    for nom, annees in [("Fort", 8.0), ("Faible", 0.0)]:
        candidat = Candidate.objects.create(full_name=nom, total_experience_years=annees)
        if annees:
            CandidateSkill.objects.create(
                candidate=candidat, name="Python", years=annees, last_used_year=2026
            )
        score_application(
            Application.objects.create(candidate=candidat, offer=offre),
            with_explanation=False,
        )

    client.force_login(recruteur)
    contexte = client.get(
        reverse("matching:ranking", kwargs={"slug": offre.slug})
    ).context

    assert contexte["cut_percentage"] > 0
    # Un seul marqueur de coupe, quel que soit le nombre de lignes en dessous.
    assert sum(1 for ligne in contexte["rows"] if ligne["cut_before"]) <= 1


def test_the_cut_marker_appears_once_with_many_rows_below(client, db, recruteur):
    from apps.candidates.models import Application, Candidate
    from apps.jobs.models import JobOffer, JobSkill
    from apps.matching.services import score_application

    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    JobSkill.objects.create(offer=offre, name="Kubernetes", requirement="required")
    for index in range(4):
        candidat = Candidate.objects.create(full_name=f"Faible {index}")
        score_application(
            Application.objects.create(candidate=candidat, offer=offre),
            with_explanation=False,
        )

    client.force_login(recruteur)
    lignes = client.get(
        reverse("matching:ranking", kwargs={"slug": offre.slug})
    ).context["rows"]

    assert len(lignes) == 4
    assert sum(1 for ligne in lignes if ligne["cut_before"]) == 1
    assert lignes[0]["cut_before"] is True
