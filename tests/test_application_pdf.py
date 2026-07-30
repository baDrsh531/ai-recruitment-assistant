"""Tests du dossier de candidature en PDF.

Ce document sort du systeme avec des donnees personnelles dedans. Les tests
portent donc autant sur ce qu'il contient que sur ce qu'il ne doit pas
contenir : en screening a l'aveugle, l'identite doit rester masquee, sans quoi
l'export deviendrait la porte de sortie que l'attenuation du biais cherche a
fermer.
"""

from __future__ import annotations

import datetime as dt

import fitz
import pytest
from django.urls import reverse

from apps.candidates.models import Application, Candidate, CandidateSkill
from apps.core.models import AuditLog
from apps.evaluation import report_pdf
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching.models import InterviewQuestion
from apps.matching.services import decide, score_application


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123", role="recruiter",
        first_name="Nadia", last_name="Cherkaoui",
    )


@pytest.fixture
def candidature(db):
    offre = JobOffer.objects.create(
        title="Ingenieur Backend", description="x", status="open"
    )
    JobSkill.objects.create(offer=offre, name="Python", requirement="required")
    JobSkill.objects.create(offer=offre, name="Kubernetes", requirement="required")
    candidat = Candidate.objects.create(
        full_name="Alice Martin", email="alice@example.com", total_experience_years=4
    )
    CandidateSkill.objects.create(
        candidate=candidat, name="Python", years=4, last_used_year=2026
    )
    return Application.objects.create(candidate=candidat, offer=offre)


def _texte(octets: bytes) -> str:
    document = fitz.open(stream=octets, filetype="pdf")
    brut = "\n".join(page.get_text() for page in document)
    document.close()
    return report_pdf.texte_normalise(brut)


# --- Contenu -----------------------------------------------------------------
def test_the_file_names_the_candidate_and_the_role(candidature):
    octets = report_pdf.build_application(candidature, author="Nadia Cherkaoui")
    texte = _texte(octets)

    assert "Alice Martin" in texte
    assert "Ingenieur Backend" in texte
    assert "Nadia Cherkaoui" in texte


def test_an_unscored_application_says_so(candidature):
    texte = _texte(report_pdf.build_application(candidature))
    assert "n'a pas encore ete scoree" in texte


def test_the_score_is_broken_down_by_criterion(candidature):
    score = score_application(candidature, with_explanation=False)
    texte = _texte(report_pdf.build_application(candidature, score=score))

    assert f"{score.percentage} %" in texte
    assert "Competences" in texte
    assert score.engine_version in texte


def test_the_gaps_are_listed_with_their_caveat(candidature):
    score = score_application(candidature, with_explanation=False)
    texte = _texte(report_pdf.build_application(candidature, score=score))

    assert "Kubernetes" in texte
    assert "n'est pas un motif de rejet" in texte


def test_a_manual_override_is_disclosed(candidature, recruteur):
    from apps.matching.services import override_score

    score = score_application(candidature, with_explanation=False)
    override_score(score, value=0.42, reason="Entretien decevant", actor=recruteur)
    texte = _texte(report_pdf.build_application(candidature, score=score))

    assert "corrige manuellement" in texte
    assert "Entretien decevant" in texte


def test_decisions_are_reproduced(candidature, recruteur):
    decide(candidature, stage="screening", note="Profil coherent", actor=recruteur)
    entrees = AuditLog.objects.filter(
        action=AuditLog.Action.STAGE_CHANGED, object_id=str(candidature.pk)
    )
    texte = _texte(report_pdf.build_application(candidature, decisions=entrees))

    assert "Profil coherent" in texte
    assert "Nadia Cherkaoui" in texte


def test_without_a_decision_the_principle_is_restated(candidature):
    texte = _texte(report_pdf.build_application(candidature))
    assert "il n'en ecarte aucune" in texte


def test_interview_questions_appear_with_their_anchor(candidature):
    question = InterviewQuestion.objects.create(
        application=candidature,
        theme="Orchestration",
        question="Comment avez-vous gere la montee en charge ?",
        cv_claim="Mise a l'echelle d'un cluster de 40 noeuds",
        model="qwen3.6-35b",
    )
    texte = _texte(report_pdf.build_application(candidature, questions=[question]))

    assert "Orchestration" in texte
    assert "montee en charge" in texte
    assert "40 noeuds" in texte


# --- Screening a l'aveugle ---------------------------------------------------
def test_blind_export_masks_the_identity(candidature):
    """Sans cela, l'export serait la porte de sortie du screening a l'aveugle."""
    octets = report_pdf.build_application(candidature, blind=True)
    texte = _texte(octets)

    assert "Alice" not in texte
    assert "Martin" not in texte
    assert "Candidat" in texte


def test_blind_export_also_masks_the_metadata(candidature):
    """Le nom se lit aussi dans les proprietes du fichier."""
    octets = report_pdf.build_application(candidature, blind=True)
    document = fitz.open(stream=octets, filetype="pdf")
    titre = document.metadata["title"]
    document.close()

    assert "Alice" not in titre
    assert "Martin" not in titre


def test_without_blind_the_name_is_present(candidature):
    texte = _texte(report_pdf.build_application(candidature, blind=False))
    assert "Alice Martin" in texte


# --- Document ----------------------------------------------------------------
def test_the_filename_carries_the_date(candidature):
    nom = report_pdf.application_filename(candidature, dt.date(2026, 3, 14))
    assert nom.endswith("_2026-03-14.pdf")
    assert str(candidature.pk)[:8] in nom


def test_every_page_is_numbered(candidature):
    octets = report_pdf.build_application(candidature)
    document = fitz.open(stream=octets, filetype="pdf")
    total = document.page_count
    for numero, page in enumerate(document, start=1):
        assert f"page {numero} / {total}" in page.get_text()
    document.close()


def test_two_builds_give_the_same_text(candidature):
    jour = dt.date(2026, 3, 14)
    premier = _texte(report_pdf.build_application(candidature, author="X", today=jour))
    second = _texte(report_pdf.build_application(candidature, author="X", today=jour))
    assert premier == second


# --- Vue ---------------------------------------------------------------------
def test_the_view_serves_a_pdf(client, candidature, recruteur):
    client.force_login(recruteur)
    reponse = client.get(
        reverse("candidates:export_application", kwargs={"pk": candidature.pk})
    )

    assert reponse.status_code == 200
    assert reponse["Content-Type"] == "application/pdf"
    assert "dossier_" in reponse["Content-Disposition"]
    assert reponse.content.startswith(b"%PDF")


def test_the_export_is_journalised_against_the_application(client, candidature, recruteur):
    client.force_login(recruteur)
    client.get(reverse("candidates:export_application", kwargs={"pk": candidature.pk}))

    entree = AuditLog.objects.filter(action=AuditLog.Action.DATA_EXPORTED).latest(
        "created_at"
    )
    assert entree.actor == recruteur
    assert entree.object_id == str(candidature.pk)
    assert entree.metadata["scope"] == "candidature"


def test_the_view_follows_the_account_blind_preference(client, candidature, recruteur):
    recruteur.blind_screening = True
    recruteur.save()
    client.force_login(recruteur)

    reponse = client.get(
        reverse("candidates:export_application", kwargs={"pk": candidature.pk})
    )
    assert "Alice" not in _texte(reponse.content)


def test_an_unknown_application_is_a_404(client, db, recruteur):
    import uuid

    client.force_login(recruteur)
    reponse = client.get(
        reverse("candidates:export_application", kwargs={"pk": uuid.uuid4()})
    )
    assert reponse.status_code == 404


def test_an_anonymous_caller_is_refused(client, candidature):
    reponse = client.get(
        reverse("candidates:export_application", kwargs={"pk": candidature.pk})
    )
    assert reponse.status_code == 302
    assert not AuditLog.objects.filter(action=AuditLog.Action.DATA_EXPORTED).exists()
