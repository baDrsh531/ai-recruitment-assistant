"""Tests du rapprochement et de la fusion de dossiers.

Le point sensible n'est pas de trouver les doublons : c'est de ne pas fusionner
deux personnes distinctes, et de ne rien perdre quand la fusion est justifiee.
Ces tests portent surtout la-dessus.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from apps.candidates import duplicates
from apps.candidates.models import (
    Application,
    Candidate,
    CandidateLanguage,
    CandidateSkill,
    Experience,
)
from apps.core.models import AuditLog
from apps.jobs.models import JobOffer


@pytest.fixture
def offre(db):
    return JobOffer.objects.create(title="Backend", description="x", status="open")


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


# --- Cles de rapprochement ---------------------------------------------------
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Badr Sahraoui", "SAHRAOUI Badr"),
        ("Badr Sahraoui", "badr  sahraoui"),
        ("Béatrice Lefèvre", "Beatrice Lefevre"),
        ("Jean-Pierre Martin", "Martin Jean Pierre"),
    ],
)
def test_the_name_key_survives_order_case_and_accents(a, b):
    assert duplicates.cle_nom(a) == duplicates.cle_nom(b)


def test_the_name_key_separates_different_people():
    assert duplicates.cle_nom("Badr Sahraoui") != duplicates.cle_nom("Badr Sahraouy")


def test_particles_do_not_carry_identity():
    """« de », « el », « ben » n'aident pas a distinguer deux personnes."""
    assert duplicates.cle_nom("Sara El Amrani") == duplicates.cle_nom("Sara Amrani")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("+212 600 112 233", "0600112233"),
        ("06.00.11.22.33", "00212600112233"),
    ],
)
def test_the_phone_key_ignores_formatting_and_prefix(a, b):
    assert duplicates.cle_telephone(a) == duplicates.cle_telephone(b)


def test_a_short_number_yields_no_key():
    """Un numero tronque rapprocherait n'importe qui : mieux vaut aucune cle."""
    assert duplicates.cle_telephone("12345") == ""


# --- Rapprochement -----------------------------------------------------------
def test_a_shared_address_is_enough(db):
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    Candidate.objects.create(full_name="A. Martin", email="A@Example.com")

    groupes = duplicates.scan()

    assert len(groupes) == 1
    assert groupes[0].size == 2
    assert any("meme adresse" in raison for raison in groupes[0].reasons)


def test_a_shared_name_alone_is_not_enough(db):
    """Deux homonymes sont deux personnes tant que rien d'autre ne les relie."""
    Candidate.objects.create(full_name="Mohamed Alami", email="m.alami@example.com")
    Candidate.objects.create(full_name="Mohamed Alami", email="mohamed.a@autre.com")

    assert duplicates.scan() == []


def test_a_shared_name_plus_a_shared_employer_is_enough(db):
    premier = Candidate.objects.create(full_name="Mohamed Alami", email="m@a.com")
    second = Candidate.objects.create(full_name="Alami Mohamed", email="m2@b.com")
    for candidat in (premier, second):
        Experience.objects.create(candidate=candidat, title="Dev", company="Atos")

    groupes = duplicates.scan()

    assert len(groupes) == 1
    assert any("employeur" in raison for raison in groupes[0].reasons)


def test_a_shared_name_plus_a_shared_phone_is_enough(db):
    Candidate.objects.create(
        full_name="Sara Idrissi", email="s@a.com", phone="+212 600 112 233"
    )
    Candidate.objects.create(
        full_name="Idrissi Sara", email="s@b.com", phone="0600112233"
    )

    groupes = duplicates.scan()
    assert len(groupes) == 1
    assert groupes[0].confidence >= duplicates.SEUIL


def test_unrelated_records_are_not_grouped(db):
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    Candidate.objects.create(full_name="Bob Durand", email="b@example.com")

    assert duplicates.scan() == []


def test_three_records_form_a_single_group(db):
    for suffixe in range(3):
        Candidate.objects.create(
            full_name=f"Karim Benjelloun {suffixe}", email="karim@example.com"
        )

    groupes = duplicates.scan()
    assert len(groupes) == 1
    assert groupes[0].size == 3


def test_the_oldest_record_is_proposed_for_keeping(db):
    ancien = Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")

    groupe = duplicates.scan()[0]
    assert groupe.primary.pk == ancien.pk
    assert len(groupe.others) == 1


def test_scanning_changes_nothing(db):
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")

    duplicates.scan()

    assert Candidate.objects.count() == 2


def test_an_empty_base_yields_nothing(db):
    assert duplicates.scan() == []


# --- Fusion ------------------------------------------------------------------
def test_merging_keeps_everything(db, offre, recruteur):
    garde = Candidate.objects.create(
        full_name="Alice Martin", email="a@example.com", total_experience_years=3
    )
    CandidateSkill.objects.create(candidate=garde, name="Python", years=3)
    autre = Candidate.objects.create(
        full_name="Alice Martin", phone="0600112233", total_experience_years=5
    )
    CandidateSkill.objects.create(candidate=autre, name="Django", years=2)
    Experience.objects.create(candidate=autre, title="Dev", company="Atos")
    CandidateLanguage.objects.create(candidate=autre, language="Anglais", level="C1")

    duplicates.merge(garde, [autre], actor=recruteur)
    garde.refresh_from_db()

    assert Candidate.objects.count() == 1
    assert set(garde.skills.values_list("name", flat=True)) == {"Python", "Django"}
    assert garde.experiences.count() == 1
    assert garde.languages.count() == 1
    # Les champs vides du dossier conserve sont completes par l'autre.
    assert garde.phone == "0600112233"
    assert garde.email == "a@example.com"
    assert garde.total_experience_years == 5


def test_a_skill_present_on_both_sides_keeps_the_best_seniority(db, recruteur):
    garde = Candidate.objects.create(full_name="Alice", email="a@example.com")
    CandidateSkill.objects.create(
        candidate=garde, name="Python", years=2, last_used_year=2022
    )
    autre = Candidate.objects.create(full_name="Alice", email="a@example.com")
    CandidateSkill.objects.create(
        candidate=autre, name="Python", years=6, last_used_year=2026
    )

    duplicates.merge(garde, [autre], actor=recruteur)

    competence = garde.skills.get(name="Python")
    assert competence.years == 6
    assert competence.last_used_year == 2026


def test_the_most_advanced_application_wins(db, offre, recruteur):
    """Perdre un entretien passe parce qu'on a fusionne serait le pire resultat."""
    garde = Candidate.objects.create(full_name="Alice", email="a@example.com")
    Application.objects.create(candidate=garde, offer=offre, stage="received")
    autre = Candidate.objects.create(full_name="Alice", email="a@example.com")
    Application.objects.create(candidate=autre, offer=offre, stage="technical")

    duplicates.merge(garde, [autre], actor=recruteur)

    assert garde.applications.count() == 1
    assert garde.applications.first().stage == "technical"


def test_a_less_advanced_duplicate_application_is_dropped(db, offre, recruteur):
    garde = Candidate.objects.create(full_name="Alice", email="a@example.com")
    Application.objects.create(candidate=garde, offer=offre, stage="final")
    autre = Candidate.objects.create(full_name="Alice", email="a@example.com")
    Application.objects.create(candidate=autre, offer=offre, stage="received")

    duplicates.merge(garde, [autre], actor=recruteur)

    assert garde.applications.count() == 1
    assert garde.applications.first().stage == "final"


def test_applications_on_different_offers_are_all_kept(db, offre, recruteur):
    seconde = JobOffer.objects.create(title="Data", description="x", status="open")
    garde = Candidate.objects.create(full_name="Alice", email="a@example.com")
    Application.objects.create(candidate=garde, offer=offre)
    autre = Candidate.objects.create(full_name="Alice", email="a@example.com")
    Application.objects.create(candidate=autre, offer=seconde)

    duplicates.merge(garde, [autre], actor=recruteur)

    assert garde.applications.count() == 2


def test_the_latest_retention_deadline_is_kept(db, recruteur):
    """La candidature la plus recente justifie de conserver le dossier."""
    tot = dt.date.today() + dt.timedelta(days=30)
    tard = dt.date.today() + dt.timedelta(days=300)
    garde = Candidate.objects.create(
        full_name="Alice", email="a@example.com", retention_until=tot
    )
    autre = Candidate.objects.create(
        full_name="Alice", email="a@example.com", retention_until=tard
    )

    duplicates.merge(garde, [autre], actor=recruteur)
    garde.refresh_from_db()

    assert garde.retention_until == tard


def test_merging_is_journalised(db, recruteur):
    garde = Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    autre = Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    identifiant = str(autre.pk)

    duplicates.merge(garde, [autre], actor=recruteur)

    entree = AuditLog.objects.get(action=AuditLog.Action.CANDIDATES_MERGED)
    assert entree.actor == recruteur
    assert identifiant in entree.metadata["merged"]
    assert entree.metadata["kept"] == str(garde.pk)


def test_a_viewer_cannot_merge(db, observateur):
    garde = Candidate.objects.create(full_name="Alice", email="a@example.com")
    autre = Candidate.objects.create(full_name="Alice", email="a@example.com")

    with pytest.raises(duplicates.MergeRefused, match="habilite"):
        duplicates.merge(garde, [autre], actor=observateur)

    assert Candidate.objects.count() == 2


def test_merging_nothing_is_refused(db, recruteur):
    garde = Candidate.objects.create(full_name="Alice", email="a@example.com")
    with pytest.raises(duplicates.MergeRefused, match="Aucun dossier"):
        duplicates.merge(garde, [garde], actor=recruteur)


# --- Interface ---------------------------------------------------------------
def test_the_page_lists_the_groups(client, db, recruteur):
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")

    client.force_login(recruteur)
    reponse = client.get(reverse("candidates:duplicates"))

    assert reponse.status_code == 200
    assert reponse.context["stats"]["groups"] == 1
    assert reponse.context["stats"]["records"] == 2


def test_the_page_merges_on_post(client, db, recruteur):
    garde = Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    autre = Candidate.objects.create(full_name="Alice Martin", email="a@example.com")

    client.force_login(recruteur)
    client.post(
        reverse("candidates:merge"),
        {"keep": str(garde.pk), "merge": [str(garde.pk), str(autre.pk)]},
        follow=True,
    )

    assert Candidate.objects.count() == 1


def test_a_viewer_is_refused_on_the_merge_view(client, db, observateur):
    garde = Candidate.objects.create(full_name="Alice", email="a@example.com")
    autre = Candidate.objects.create(full_name="Alice", email="a@example.com")

    client.force_login(observateur)
    reponse = client.post(
        reverse("candidates:merge"),
        {"keep": str(garde.pk), "merge": [str(autre.pk)]},
        HTTP_REFERER="/", follow=True,
    )

    messages = [str(m) for m in reponse.context["messages"]]
    assert any("role" in message.lower() for message in messages)
    assert Candidate.objects.count() == 2


def test_a_viewer_can_read_the_page(client, db, observateur):
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")
    Candidate.objects.create(full_name="Alice Martin", email="a@example.com")

    client.force_login(observateur)
    contenu = client.get(reverse("candidates:duplicates")).content.decode()

    assert "lecture seule" in contenu
    assert 'name="keep"' not in contenu
