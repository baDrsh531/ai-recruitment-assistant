"""Tests de la redirection positive inter-offres.

Le point du module n'est pas de trouver des offres : c'est de ne pas laisser
tomber un dossier sans avoir regarde ailleurs. Les tests portent donc autant
sur ce qui est propose que sur ce qui ne l'est pas — une offre fermee, une
offre ou le candidat ne passe pas davantage, ou l'offre d'origine elle-meme.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching import redirect as redirect_offers
from apps.matching.services import score_application


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


def _offre(titre, competences, *, statut="open"):
    offre = JobOffer.objects.create(title=titre, description="x", status=statut)
    for nom in competences:
        JobSkill.objects.create(offer=offre, name=nom, requirement="required")
    return offre


def _candidat(nom, competences):
    candidat = Candidate.objects.create(full_name=nom, total_experience_years=5)
    for nom_competence in competences:
        CandidateSkill.objects.create(
            candidate=candidat, name=nom_competence, years=5, last_used_year=2026
        )
    return candidat


@pytest.fixture
def scene(db):
    """Un profil data qui a postule a une offre backend : il passe ailleurs."""
    backend = _offre("Backend Python", ["Django", "PostgreSQL"])
    data = _offre("Data Engineer", ["SQL", "Airflow"])
    candidat = _candidat("Sara", ["SQL", "Airflow", "Python"])
    candidature = Application.objects.create(candidate=candidat, offer=backend)
    score_application(candidature, with_explanation=False)
    return candidature, backend, data


# --- Comportement de base ----------------------------------------------------
def test_a_candidate_below_the_threshold_is_offered_elsewhere(scene):
    candidature, _, data = scene
    resultat = redirect_offers.for_application(candidature, threshold=0.6)

    assert resultat.below_threshold
    assert [item.offer.pk for item in resultat.suggestions] == [data.pk]


def test_the_suggestion_carries_what_justifies_it(scene):
    candidature, _, _ = scene
    suggestion = redirect_offers.for_application(candidature, threshold=0.6).suggestions[0]

    assert set(suggestion.matched_skills) == {"SQL", "Airflow"}
    assert suggestion.gain > 0
    assert suggestion.percentage >= 60


def test_a_candidate_above_the_threshold_gets_no_suggestion(db):
    """Il passe la ou il est : il n'y a rien a rattraper."""
    backend = _offre("Backend Python", ["Django"])
    _offre("Data Engineer", ["SQL"])
    candidat = _candidat("Ahmed", ["Django", "SQL"])
    candidature = Application.objects.create(candidate=candidat, offer=backend)
    score_application(candidature, with_explanation=False)

    resultat = redirect_offers.for_application(candidature, threshold=0.5)

    assert not resultat.below_threshold
    assert resultat.suggestions == []
    assert resultat.offers_examined == 0, "aucune offre n'a besoin d'etre examinee"


def test_the_original_offer_is_never_suggested(scene):
    candidature, backend, _ = scene
    resultat = redirect_offers.for_application(candidature, threshold=0.6)

    assert all(item.offer.pk != backend.pk for item in resultat.suggestions)


def test_a_closed_offer_is_never_suggested(db):
    backend = _offre("Backend Python", ["Django"])
    _offre("Data Engineer", ["SQL"], statut="closed")
    candidat = _candidat("Sara", ["SQL"])
    candidature = Application.objects.create(candidate=candidat, offer=backend)
    score_application(candidature, with_explanation=False)

    resultat = redirect_offers.for_application(candidature, threshold=0.6)

    assert resultat.suggestions == []
    assert resultat.offers_examined == 0


def test_an_offer_where_the_candidate_does_no_better_is_not_suggested(db):
    backend = _offre("Backend Python", ["Django"])
    _offre("Infrastructure", ["Kubernetes", "Terraform"])
    candidat = _candidat("Sara", ["SQL"])
    candidature = Application.objects.create(candidate=candidat, offer=backend)
    score_application(candidature, with_explanation=False)

    resultat = redirect_offers.for_application(candidature, threshold=0.6)

    assert resultat.offers_examined == 1
    assert resultat.suggestions == []


def test_an_existing_application_elsewhere_is_flagged(scene):
    """Suggerer une offre ou il a deja postule serait du bruit : on le dit."""
    candidature, _, data = scene
    Application.objects.create(candidate=candidature.candidate, offer=data)

    suggestion = redirect_offers.for_application(candidature, threshold=0.6).suggestions[0]
    assert suggestion.already_applied is True


def test_suggestions_are_ordered_by_score(db):
    origine = _offre("Backend Python", ["Django"])
    _offre("Data A", ["SQL"])
    _offre("Data B", ["SQL", "Airflow"])
    candidat = _candidat("Sara", ["SQL", "Airflow"])
    candidature = Application.objects.create(candidate=candidat, offer=origine)
    score_application(candidature, with_explanation=False)

    suggestions = redirect_offers.for_application(candidature, threshold=0.5).suggestions
    scores = [item.score for item in suggestions]
    assert scores == sorted(scores, reverse=True)


def test_the_list_is_bounded(db):
    origine = _offre("Backend Python", ["Django"])
    for index in range(8):
        _offre(f"Data {index}", ["SQL"])
    candidat = _candidat("Sara", ["SQL"])
    candidature = Application.objects.create(candidate=candidat, offer=origine)
    score_application(candidature, with_explanation=False)

    resultat = redirect_offers.for_application(candidature, threshold=0.5)
    assert len(resultat.suggestions) <= redirect_offers.MAX_SUGGESTIONS
    assert resultat.offers_examined == 8


def test_an_unscored_application_is_treated_as_below(db):
    backend = _offre("Backend Python", ["Django"])
    _offre("Data Engineer", ["SQL"])
    candidat = _candidat("Sara", ["SQL"])
    candidature = Application.objects.create(candidate=candidat, offer=backend)

    resultat = redirect_offers.for_application(candidature, threshold=0.6)

    assert resultat.current_score == 0.0
    assert resultat.below_threshold
    assert resultat.suggestions


# --- Rien n'est cree ---------------------------------------------------------
def test_nothing_is_created(scene):
    """Postuler ailleurs appartient au candidat, pas a l'outil."""
    candidature, _, _ = scene
    avant = Application.objects.count()

    redirect_offers.for_application(candidature, threshold=0.6)

    assert Application.objects.count() == avant


# --- Vue d'ensemble par offre ------------------------------------------------
def test_the_offer_view_lists_only_redirectable_applications(db):
    backend = _offre("Backend Python", ["Django"])
    _offre("Data Engineer", ["SQL"])

    redirigeable = _candidat("Sara", ["SQL"])
    passe = _candidat("Ahmed", ["Django"])
    for candidat in (redirigeable, passe):
        candidature = Application.objects.create(candidate=candidat, offer=backend)
        score_application(candidature, with_explanation=False)

    resultats = redirect_offers.for_offer(backend, threshold=0.6)

    noms = {item.application.candidate.full_name for item in resultats}
    assert noms == {"Sara"}


def test_a_withdrawn_application_is_left_alone(db):
    backend = _offre("Backend Python", ["Django"])
    _offre("Data Engineer", ["SQL"])
    candidat = _candidat("Sara", ["SQL"])
    candidature = Application.objects.create(
        candidate=candidat, offer=backend, stage="withdrawn"
    )
    score_application(candidature, with_explanation=False)

    assert redirect_offers.for_offer(backend, threshold=0.6) == []


# --- Interface ---------------------------------------------------------------
def test_the_page_shows_the_suggestion(client, scene, recruteur):
    candidature, _, _ = scene
    client.force_login(recruteur)
    reponse = client.get(
        reverse("candidates:application_detail", kwargs={"pk": candidature.pk})
    )

    # Sans apostrophe dans l'assertion : le texte litteral d'un gabarit n'est
    # pas echappe, seules les variables le sont, et un test qui depend de la
    # forme de l'echappement casse pour une raison sans rapport.
    assert reponse.context["redirection"].below_threshold
    contenu = reponse.content.decode()
    assert "Ailleurs dans" in contenu
    assert "Data Engineer" in contenu


def test_the_page_says_when_it_looked_and_found_nothing(client, db, recruteur):
    """« Aucune autre offre » et « on n'a pas regarde » ne se confondent pas."""
    backend = _offre("Backend Python", ["Django"])
    _offre("Infrastructure", ["Kubernetes"])
    candidat = _candidat("Sara", ["SQL"])
    candidature = Application.objects.create(candidate=candidat, offer=backend)
    score_application(candidature, with_explanation=False)

    client.force_login(recruteur)
    contenu = client.get(
        reverse("candidates:application_detail", kwargs={"pk": candidature.pk})
    ).content.decode()

    assert "La verification a eu lieu" in contenu


def test_the_report_serialises(scene):
    donnees = redirect_offers.for_application(scene[0], threshold=0.6).as_dict()

    assert set(donnees) >= {
        "current_score", "threshold", "below_threshold", "offers_examined",
        "suggestions",
    }
    assert donnees["suggestions"][0]["title"] == "Data Engineer"
