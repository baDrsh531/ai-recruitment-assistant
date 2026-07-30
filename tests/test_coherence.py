"""Tests des controles de coherence.

Ces controles portent sur des dossiers de personnes reelles : ce qui compte
autant que leur capacite a trouver une incoherence, c'est leur retenue. Les
tests verifient donc les deux — qu'un vrai chevauchement est vu, et qu'un
preavis d'un mois, une formation continue ou une interruption de carriere ne
sont pas transformes en soupcon.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from apps.candidates import coherence
from apps.candidates.models import (
    Application,
    Candidate,
    Education,
    Experience,
)
from apps.jobs.models import JobOffer
from apps.matching import engine

AUJOURDHUI = dt.date(2026, 7, 30)


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


def _candidat(db, annees=0.0):
    return Candidate.objects.create(full_name="Alice Martin", total_experience_years=annees)


def _experience(candidat, titre, debut, fin=None, entreprise="Societe"):
    return Experience.objects.create(
        candidate=candidat,
        title=titre,
        company=entreprise,
        start_date=dt.date.fromisoformat(debut) if debut else None,
        end_date=dt.date.fromisoformat(fin) if fin else None,
    )


def _codes(rapport):
    return {item.code for item in rapport.signalements}


# --- Chevauchements ----------------------------------------------------------
def test_a_long_overlap_is_flagged(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", "2023-01-01")
    _experience(candidat, "Lead", "2021-06-01", "2024-01-01")

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)

    assert "chevauchement" in _codes(rapport)
    signalement = next(i for i in rapport.signalements if i.code == "chevauchement")
    assert signalement.gravite == "attention"
    assert "cumul" in signalement.question


def test_a_short_handover_is_not_flagged(db):
    """Un preavis d'un mois entre deux contrats n'a rien de remarquable."""
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", "2023-02-01")
    _experience(candidat, "Lead", "2023-01-15", "2024-01-01")

    assert "chevauchement" not in _codes(coherence.analyse(candidat, today=AUJOURDHUI))


def test_consecutive_jobs_are_not_flagged(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", "2022-12-31")
    _experience(candidat, "Lead", "2023-01-01", "2024-12-31")

    assert _codes(coherence.analyse(candidat, today=AUJOURDHUI)) == set()


def test_a_current_job_overlapping_a_past_one_is_flagged(db):
    """Un poste sans date de fin court jusqu'a aujourd'hui."""
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", None)
    _experience(candidat, "Lead", "2022-01-01", "2024-01-01")

    assert "chevauchement" in _codes(coherence.analyse(candidat, today=AUJOURDHUI))


# --- Dates impossibles -------------------------------------------------------
def test_reversed_dates_are_flagged(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2023-01-01", "2020-01-01")

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)
    assert "dates_inversees" in _codes(rapport)
    assert rapport.signalements[0].gravite == "attention"


def test_a_future_start_is_flagged(db):
    candidat = _candidat(db)
    demain = (dt.date.today() + dt.timedelta(days=400)).isoformat()
    _experience(candidat, "Dev", demain)

    assert "date_future" in _codes(coherence.analyse(candidat, today=AUJOURDHUI))


# --- Diplome -----------------------------------------------------------------
def test_a_degree_obtained_long_after_the_first_job_is_noted(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2015-01-01", "2020-01-01")
    Education.objects.create(
        candidate=candidat, degree="Master informatique", graduation_year=2022
    )

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)
    signalement = next(i for i in rapport.signalements if i.code == "diplome_posterieur")
    assert signalement.gravite == "information", "une reprise d'etudes n'est pas suspecte"
    assert "formation continue" in signalement.question


def test_a_degree_obtained_before_the_first_job_is_normal(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", "2023-01-01")
    Education.objects.create(
        candidate=candidat, degree="Master informatique", graduation_year=2019
    )

    assert "diplome_posterieur" not in _codes(coherence.analyse(candidat, today=AUJOURDHUI))


def test_a_degree_during_the_first_year_is_not_flagged(db):
    """L'alternance et la fin d'etudes en poste sont courantes."""
    candidat = _candidat(db)
    _experience(candidat, "Alternant", "2021-09-01", "2023-01-01")
    Education.objects.create(candidate=candidat, degree="Master", graduation_year=2022)

    assert "diplome_posterieur" not in _codes(coherence.analyse(candidat, today=AUJOURDHUI))


# --- Anciennete --------------------------------------------------------------
def test_declared_seniority_far_above_the_dated_periods_is_noted(db):
    candidat = _candidat(db, annees=15.0)
    _experience(candidat, "Dev", "2022-01-01", "2024-01-01")

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)
    signalement = next(
        i for i in rapport.signalements if i.code == "anciennete_non_couverte"
    )
    assert "non extraites" in signalement.question


def test_overlapping_periods_are_counted_once(db):
    """Comme dans le moteur : deux missions en parallele ne font pas double."""
    candidat = _candidat(db, annees=3.0)
    _experience(candidat, "Dev", "2021-01-01", "2024-01-01")
    _experience(candidat, "Conseil", "2021-06-01", "2023-06-01")

    assert "anciennete_non_couverte" not in _codes(
        coherence.analyse(candidat, today=AUJOURDHUI)
    )


def test_a_small_gap_in_seniority_is_tolerated(db):
    candidat = _candidat(db, annees=4.0)
    _experience(candidat, "Dev", "2022-01-01", "2025-01-01")

    assert "anciennete_non_couverte" not in _codes(
        coherence.analyse(candidat, today=AUJOURDHUI)
    )


# --- Interruptions -----------------------------------------------------------
def test_a_long_gap_is_reported_as_information_only(db):
    """Une interruption n'est jamais un signal negatif."""
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2018-01-01", "2019-01-01")
    _experience(candidat, "Lead", "2021-01-01", "2023-01-01")

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)
    signalement = next(i for i in rapport.signalements if i.code == "interruption")

    assert signalement.gravite == "information"
    assert "jamais peser sur la decision" in signalement.question


def test_a_short_gap_is_not_reported(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", "2023-01-01")
    _experience(candidat, "Lead", "2023-03-01", "2024-01-01")

    assert "interruption" not in _codes(coherence.analyse(candidat, today=AUJOURDHUI))


# --- Retenue generale --------------------------------------------------------
def test_a_cv_without_dates_is_declared_unverifiable(db):
    """« Aucune incoherence » sur un CV sans dates serait un mensonge."""
    candidat = _candidat(db)
    _experience(candidat, "Dev", None)
    _experience(candidat, "Lead", None)

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)

    assert not rapport.verifiable
    assert rapport.signalements == []
    assert rapport.coverage == 0.0


def test_a_fully_dated_cv_is_verifiable(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", "2022-01-01")
    _experience(candidat, "Lead", "2022-02-01", "2024-01-01")

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)
    assert rapport.verifiable
    assert rapport.coverage == 1.0


def test_findings_put_what_needs_checking_first(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2018-01-01", "2019-01-01")
    _experience(candidat, "Lead", "2021-01-01", "2024-06-01")
    _experience(candidat, "Conseil", "2022-01-01", "2024-01-01")

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)
    gravites = [item.gravite for item in rapport.signalements]

    assert "attention" in gravites
    assert gravites[0] == "attention"


def test_the_report_never_touches_the_score(db):
    """Le classement reste le fait du moteur deterministe."""
    candidat = _candidat(db, annees=15.0)
    _experience(candidat, "Dev", "2023-01-01", "2020-01-01")

    coherence.analyse(candidat, today=AUJOURDHUI)
    candidat.refresh_from_db()

    assert candidat.total_experience_years == 15.0
    assert Experience.objects.count() == 1


def test_the_module_does_not_guess_at_generated_text(db):
    """Un detecteur de texte genere sur-signale les locuteurs non natifs.

    Ce test verrouille une decision de conception : aucun signalement ne peut
    porter sur le style d'ecriture, faute de pouvoir etre verifie ni explique
    a un candidat.
    """
    candidat = _candidat(db)
    _experience(
        candidat, "Dev", "2020-01-01", "2023-01-01",
        entreprise="Societe",
    )
    Experience.objects.filter(candidate=candidat).update(
        description="Leveraged synergies to deliver impactful outcomes at scale."
    )

    rapport = coherence.analyse(candidat, today=AUJOURDHUI)
    interdits = {"texte_genere", "style_suspect", "ia_detectee"}
    assert not (_codes(rapport) & interdits)


def test_the_report_serialises(db):
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", "2023-01-01")

    donnees = coherence.analyse(candidat, today=AUJOURDHUI).as_dict()
    assert set(donnees) >= {"count", "verifiable", "coverage", "findings"}


# --- Interface ---------------------------------------------------------------
def test_the_page_shows_the_findings(client, db, recruteur):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    candidat = _candidat(db)
    _experience(candidat, "Dev", "2020-01-01", "2023-01-01")
    _experience(candidat, "Lead", "2021-06-01", "2024-01-01")
    candidature = Application.objects.create(candidate=candidat, offer=offre)

    client.force_login(recruteur)
    reponse = client.get(
        reverse("candidates:application_detail", kwargs={"pk": candidature.pk})
    )

    assert reponse.context["coherence"].count >= 1
    contenu = reponse.content.decode()
    assert "Coherence du parcours" in contenu
    assert "A demander" in contenu


def test_the_page_distinguishes_clean_from_unverifiable(client, db, recruteur):
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    candidat = _candidat(db)
    _experience(candidat, "Dev", None)
    candidature = Application.objects.create(candidate=candidat, offer=offre)

    client.force_login(recruteur)
    contenu = client.get(
        reverse("candidates:application_detail", kwargs={"pk": candidature.pk})
    ).content.decode()

    assert "non verifiable" in contenu
    assert "rien a signaler" not in contenu
