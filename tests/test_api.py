"""Tests de l'API REST.

DRF etait installe, configure et annonce dans le schema d'architecture, sans
une seule route. Ces tests verifient que l'API existe, et surtout qu'elle
n'ouvre pas une porte derobee sur les garanties de l'interface : meme controle
de role, meme obligation de motiver un rejet, meme screening a l'aveugle.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.core.models import AuditLog
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching.services import score_application


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture
def offre(db):
    offer = JobOffer.objects.create(
        title="Backend Python", description="x", status="open", location="Casablanca"
    )
    JobSkill.objects.create(offer=offer, name="Python", requirement="required")
    JobSkill.objects.create(offer=offer, name="Django", requirement="required")
    return offer


@pytest.fixture
def candidature(offre):
    candidate = Candidate.objects.create(
        full_name="Alice Martin",
        email="alice@example.com",
        phone="0600000000",
        location="Casablanca",
        linkedin_url="https://linkedin.com/in/alice",
        total_experience_years=5,
    )
    CandidateSkill.objects.create(candidate=candidate, name="Python", years=5)
    CandidateSkill.objects.create(candidate=candidate, name="Django", years=4)
    return Application.objects.create(candidate=candidate, offer=offre)


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter"
    )


@pytest.fixture
def observateur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="obs", password="mot-de-passe-de-test-123", role="viewer"
    )


# --- Acces -------------------------------------------------------------------
def test_the_api_refuses_an_anonymous_caller(client, db):
    assert client.get(reverse("api:root")).status_code in (401, 403)
    assert client.get(reverse("api:joboffer-list")).status_code in (401, 403)


def test_the_root_lists_what_is_exposed(client, recruteur):
    client.force_login(recruteur)
    donnees = client.get(reverse("api:root")).json()
    assert set(donnees) >= {"offres", "candidats", "candidatures"}


def test_reading_is_open_to_a_viewer(client, candidature, observateur):
    """Le controle porte sur l'ecriture : consulter reste ouvert, comme a l'ecran."""
    client.force_login(observateur)
    for nom in ["api:joboffer-list", "api:candidate-list", "api:application-list"]:
        assert client.get(reverse(nom)).status_code == 200


# --- Offres et classement ----------------------------------------------------
def test_an_offer_exposes_its_weights_and_skills(client, offre, recruteur):
    client.force_login(recruteur)
    donnees = client.get(
        reverse("api:joboffer-detail", kwargs={"slug": offre.slug})
    ).json()

    assert donnees["title"] == "Backend Python"
    assert round(sum(donnees["weights"].values()), 4) == 1.0
    assert {skill["name"] for skill in donnees["skills"]} == {"Python", "Django"}


def test_the_ranking_numbers_the_rows_itself(client, candidature, offre, recruteur):
    autre = Candidate.objects.create(full_name="Bob Durand", total_experience_years=1)
    CandidateSkill.objects.create(candidate=autre, name="Python", years=1)
    seconde = Application.objects.create(candidate=autre, offer=offre)
    score_application(candidature, with_explanation=False)
    score_application(seconde, with_explanation=False)

    client.force_login(recruteur)
    donnees = client.get(
        reverse("api:joboffer-ranking", kwargs={"slug": offre.slug})
    ).json()

    assert donnees["count"] == 2
    assert [ligne["rank"] for ligne in donnees["results"]] == [1, 2]
    # Le rang suit le score : c'est le classement qui doit etre reproductible.
    scores = [ligne["effective_score"] for ligne in donnees["results"]]
    assert scores == sorted(scores, reverse=True)
    assert donnees["results"][0]["candidate"]["full_name"] == "Alice Martin"


def test_the_ranking_counts_the_unscored(client, candidature, offre, recruteur):
    client.force_login(recruteur)
    donnees = client.get(
        reverse("api:joboffer-ranking", kwargs={"slug": offre.slug})
    ).json()
    assert donnees["count"] == 0
    assert donnees["unscored"] == 1


def test_rescoring_is_refused_to_a_viewer(client, candidature, offre, observateur):
    client.force_login(observateur)
    reponse = client.post(
        reverse("api:joboffer-rescore", kwargs={"slug": offre.slug}), {}
    )
    assert reponse.status_code == 403
    assert "role" in reponse.json()["detail"].lower()


def test_a_refused_call_is_journalised(client, offre, observateur):
    client.force_login(observateur)
    client.post(reverse("api:joboffer-rescore", kwargs={"slug": offre.slug}), {})

    entree = AuditLog.objects.filter(summary__startswith="Action refusee").latest(
        "created_at"
    )
    assert entree.actor == observateur
    assert entree.metadata["interface"] == "api"


def test_a_recruiter_can_rescore(client, candidature, offre, recruteur):
    client.force_login(recruteur)
    donnees = client.post(
        reverse("api:joboffer-rescore", kwargs={"slug": offre.slug}), {}
    ).json()
    assert donnees["scored"] == 1
    assert candidature.scores.count() == 1


# --- Decision ----------------------------------------------------------------
def test_deciding_through_the_api_moves_the_application(client, candidature, recruteur):
    client.force_login(recruteur)
    reponse = client.post(
        reverse("api:application-decide", kwargs={"pk": candidature.pk}),
        {"stage": "phone", "note": "A rappeler"},
        content_type="application/json",
    )
    assert reponse.status_code == 200
    candidature.refresh_from_db()
    assert candidature.stage == "phone"
    assert candidature.decided_by == recruteur


def test_rejecting_through_the_api_still_requires_a_reason(client, candidature, recruteur):
    """La garantie compte surtout ici : un client automatise ne doit pas la contourner."""
    client.force_login(recruteur)
    reponse = client.post(
        reverse("api:application-decide", kwargs={"pk": candidature.pk}),
        {"stage": "rejected", "note": ""},
        content_type="application/json",
    )
    assert reponse.status_code == 400
    assert "motif" in reponse.json()["detail"]
    candidature.refresh_from_db()
    assert candidature.stage == "received"


def test_an_unknown_stage_is_rejected_by_the_serializer(client, candidature, recruteur):
    client.force_login(recruteur)
    reponse = client.post(
        reverse("api:application-decide", kwargs={"pk": candidature.pk}),
        {"stage": "teleportation", "note": "x" * 20},
        content_type="application/json",
    )
    assert reponse.status_code == 400
    assert "stage" in reponse.json()


def test_a_viewer_cannot_decide_through_the_api(client, candidature, observateur):
    client.force_login(observateur)
    reponse = client.post(
        reverse("api:application-decide", kwargs={"pk": candidature.pk}),
        {"stage": "rejected", "note": "tentative depuis un client automatise"},
        content_type="application/json",
    )
    assert reponse.status_code == 403
    candidature.refresh_from_db()
    assert candidature.stage == "received"


def test_every_decision_taken_through_the_api_is_journalised(client, candidature, recruteur):
    client.force_login(recruteur)
    client.post(
        reverse("api:application-decide", kwargs={"pk": candidature.pk}),
        {"stage": "screening", "note": "Profil coherent"},
        content_type="application/json",
    )
    assert AuditLog.objects.filter(
        action=AuditLog.Action.STAGE_CHANGED, object_id=str(candidature.pk)
    ).count() == 1


# --- Screening a l'aveugle ---------------------------------------------------
def test_the_api_masks_the_name_when_the_account_screens_blind(
    client, candidature, recruteur
):
    recruteur.blind_screening = True
    recruteur.save()
    client.force_login(recruteur)

    donnees = client.get(
        reverse("api:candidate-detail", kwargs={"pk": candidature.candidate.pk})
    ).json()

    assert "Alice" not in donnees["full_name"]
    assert donnees["blind"] is True


def test_the_api_does_not_return_what_the_interface_hides(client, candidature, recruteur):
    """Masquer le nom en laissant l'e-mail ne masquerait rien du tout."""
    recruteur.blind_screening = True
    recruteur.save()
    client.force_login(recruteur)

    donnees = client.get(
        reverse("api:candidate-detail", kwargs={"pk": candidature.candidate.pk})
    ).json()

    for champ in ["email", "phone", "location", "linkedin_url"]:
        assert donnees[champ] == "", f"{champ} devrait etre retire en mode aveugle"


def test_without_blind_screening_the_fields_are_returned(client, candidature, recruteur):
    client.force_login(recruteur)
    donnees = client.get(
        reverse("api:candidate-detail", kwargs={"pk": candidature.candidate.pk})
    ).json()

    assert donnees["full_name"] == "Alice Martin"
    assert donnees["email"] == "alice@example.com"
    assert donnees["blind"] is False


def test_the_ranking_also_masks_identities(client, candidature, offre, recruteur):
    score_application(candidature, with_explanation=False)
    recruteur.blind_screening = True
    recruteur.save()
    client.force_login(recruteur)

    donnees = client.get(
        reverse("api:joboffer-ranking", kwargs={"slug": offre.slug})
    ).json()
    assert "Alice" not in donnees["results"][0]["candidate"]["full_name"]


# --- Filtres et conservation -------------------------------------------------
def test_applications_can_be_filtered_by_offer_and_stage(client, candidature, offre, recruteur):
    autre_offre = JobOffer.objects.create(title="Data", description="x", status="open")
    Application.objects.create(candidate=candidature.candidate, offer=autre_offre)

    client.force_login(recruteur)
    donnees = client.get(reverse("api:application-list"), {"offre": offre.slug}).json()
    assert donnees["count"] == 1

    donnees = client.get(reverse("api:application-list"), {"etape": "phone"}).json()
    assert donnees["count"] == 0


def test_a_candidate_carries_its_retention_deadline(client, candidature, recruteur):
    """Un consommateur doit savoir combien de temps la donnee lui reste."""
    client.force_login(recruteur)
    donnees = client.get(
        reverse("api:candidate-detail", kwargs={"pk": candidature.candidate.pk})
    ).json()

    assert donnees["retention_until"] is not None
    assert donnees["days_until_purge"] > 0


# --- Ecarts contrefactuels ---------------------------------------------------
def test_the_gaps_endpoint_returns_a_path(client, candidature, recruteur):
    client.force_login(recruteur)
    donnees = client.get(
        reverse("api:application-counterfactual", kwargs={"pk": candidature.pk})
    ).json()

    assert set(donnees) >= {"current", "target", "reached", "ceiling", "levers", "path"}
    assert donnees["offer"] == candidature.offer.slug


def test_the_gaps_endpoint_accepts_a_threshold(client, candidature, recruteur):
    client.force_login(recruteur)
    donnees = client.get(
        reverse("api:application-counterfactual", kwargs={"pk": candidature.pk}),
        {"seuil": "0.9"},
    ).json()
    assert donnees["target"] == 0.9


@pytest.mark.parametrize("valeur", ["abc", "0", "1.5", "-0.2"])
def test_the_gaps_endpoint_rejects_an_invalid_threshold(
    client, candidature, recruteur, valeur
):
    client.force_login(recruteur)
    reponse = client.get(
        reverse("api:application-counterfactual", kwargs={"pk": candidature.pk}),
        {"seuil": valeur},
    )
    assert reponse.status_code == 400


def test_the_gaps_endpoint_is_read_only_for_a_viewer(client, candidature, observateur):
    """Consulter un ecart ne modifie aucun dossier : le role lecteur y a droit."""
    client.force_login(observateur)
    reponse = client.get(
        reverse("api:application-counterfactual", kwargs={"pk": candidature.pk})
    )
    assert reponse.status_code == 200


def test_the_list_is_paginated(client, offre, recruteur):
    for index in range(3):
        Candidate.objects.create(full_name=f"Candidat {index}")
    client.force_login(recruteur)
    donnees = client.get(reverse("api:candidate-list")).json()
    assert {"count", "next", "previous", "results"} <= set(donnees)
