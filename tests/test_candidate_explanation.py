"""Tests de l'explication destinee au candidat.

Ce document part vers la personne concernee. Les tests portent donc surtout
sur ce qu'il ne doit pas contenir : les motifs internes de decision, le rang,
et toute trace des autres candidatures. Une fuite ici ne serait pas un defaut
d'affichage, ce serait la communication de donnees concernant d'autres
personnes.
"""

from __future__ import annotations

import datetime as dt

import fitz
import pytest
from django.urls import reverse

from apps.candidates.models import (
    Application,
    Candidate,
    CandidateSkill,
    CVDocument,
    EvidenceSpan,
)
from apps.core.models import AuditLog
from apps.evaluation import report_pdf
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
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
    document = CVDocument.objects.create(
        candidate=candidat, original_filename="cv.pdf", content_hash="abc123"
    )
    preuve = EvidenceSpan.objects.create(
        document=document, page=1, text="Developpement Python depuis 2020",
        verified=True, match_ratio=1.0,
    )
    CandidateSkill.objects.create(
        candidate=candidat, name="Python", years=4, last_used_year=2026,
        evidence=preuve,
    )
    return Application.objects.create(candidate=candidat, offer=offre)


def _texte(octets: bytes) -> str:
    """Texte du PDF, espaces normalises.

    L'extraction coupe aux retours a la ligne du rendu : une phrase qui tient
    sur deux lignes ne se retrouve pas telle quelle. Les espaces sont donc
    reduits a un seul, sans quoi chaque assertion dependrait de la largeur de
    la colonne.
    """
    document = fitz.open(stream=octets, filetype="pdf")
    brut = "\n".join(page.get_text() for page in document)
    document.close()
    return " ".join(report_pdf.texte_normalise(brut).split())


# --- Ce que le document contient ---------------------------------------------
def test_it_addresses_the_candidate(candidature):
    texte = _texte(report_pdf.build_candidate_explanation(candidature))

    assert "Votre candidature" in texte
    assert "Ingenieur Backend" in texte
    assert "Vos droits" in texte


def test_it_states_that_no_decision_is_automatic(candidature):
    texte = _texte(report_pdf.build_candidate_explanation(candidature))
    assert "Aucune decision vous concernant n'est prise automatiquement" in texte


def test_it_lists_the_data_kept_from_the_cv(candidature):
    texte = _texte(report_pdf.build_candidate_explanation(candidature))

    assert "Alice Martin" in texte
    assert "Competences retenues" in texte
    assert "demander la correction" in texte


def test_it_shows_where_each_skill_comes_from(candidature):
    """« Rien n'est retenu sans preuve » doit etre montrable au candidat."""
    texte = _texte(report_pdf.build_candidate_explanation(candidature))

    assert "Developpement Python depuis 2020" in texte
    assert "page 1" in texte


def test_a_profile_without_evidence_says_so_instead_of_showing_dashes(db):
    """Annoncer « rien sans preuve » puis afficher des tirets serait une
    contradiction, et c'est le candidat qui la lirait."""
    offre = JobOffer.objects.create(title="Backend", description="x", status="open")
    candidat = Candidate.objects.create(full_name="Sans CV", total_experience_years=3)
    CandidateSkill.objects.create(candidate=candidat, name="SQL", years=3)
    candidature = Application.objects.create(candidate=candidat, offer=offre)

    texte = _texte(report_pdf.build_candidate_explanation(candidature))

    assert "saisies directement" in texte
    assert "Aucune donnee n'est retenue sans" not in texte


def test_partial_evidence_accounts_for_the_rest(candidature):
    """Une competence sans extrait est expliquee, pas passee sous silence."""
    CandidateSkill.objects.create(
        candidate=candidature.candidate, name="Terraform", years=2
    )
    texte = _texte(report_pdf.build_candidate_explanation(candidature))

    assert "Developpement Python depuis 2020" in texte
    assert "sans extrait associe" in texte


def test_it_explains_the_score_in_plain_words(candidature):
    score = score_application(candidature, with_explanation=False)
    texte = _texte(report_pdf.build_candidate_explanation(candidature, score=score))

    assert f"{score.percentage} %" in texte
    assert "Competences attendues par l'offre" in texte
    assert "n'est pas une note sur votre valeur professionnelle" in texte


def test_it_frames_the_gaps_as_cv_gaps(candidature):
    """Une competence absente du CV n'est pas une competence absente du candidat."""
    score = score_application(candidature, with_explanation=False)
    texte = _texte(report_pdf.build_candidate_explanation(candidature, score=score))

    assert "Kubernetes" in texte
    assert "que votre CV ne mentionnait pas" in texte


def test_it_names_the_rights(candidature):
    texte = _texte(report_pdf.build_candidate_explanation(candidature))

    assert "reexaminee par une personne" in texte
    assert "15 et 22" in texte


def test_an_unscored_application_says_so(candidature):
    texte = _texte(report_pdf.build_candidate_explanation(candidature))
    assert "n'a pas encore ete analysee" in texte


# --- Ce que le document ne doit pas contenir ---------------------------------
def test_it_never_carries_the_decision_reason(candidature, recruteur):
    """Un motif interne n'est pas opposable au candidat sous cette forme."""
    decide(
        candidature, stage="rejected",
        note="Trop junior pour l'equipe, et pretentions trop hautes",
        actor=recruteur,
    )
    candidature.refresh_from_db()
    texte = _texte(report_pdf.build_candidate_explanation(candidature))

    assert "Trop junior" not in texte
    assert "pretentions" not in texte


def test_it_never_names_the_recruiter(candidature, recruteur):
    decide(candidature, stage="screening", note="A revoir", actor=recruteur)
    candidature.refresh_from_db()
    texte = _texte(report_pdf.build_candidate_explanation(candidature))

    assert "Nadia" not in texte
    assert "Cherkaoui" not in texte


def test_it_never_mentions_other_candidates(candidature):
    """Le rang et les autres dossiers sont des donnees d'autres personnes."""
    autre = Candidate.objects.create(full_name="Bob Durand", total_experience_years=9)
    CandidateSkill.objects.create(candidate=autre, name="Python", years=9)
    seconde = Application.objects.create(candidate=autre, offer=candidature.offer)
    score_application(seconde, with_explanation=False)
    score = score_application(candidature, with_explanation=False)

    texte = _texte(report_pdf.build_candidate_explanation(candidature, score=score))

    assert "Bob" not in texte
    assert "Durand" not in texte
    for mot in ["rang", "classement", "premier", "sur 2"]:
        assert mot not in texte.lower(), f"« {mot} » designe les autres candidatures"


def test_the_metadata_carries_no_author(candidature):
    """Le nom du recruteur se lit aussi dans les proprietes du fichier."""
    octets = report_pdf.build_candidate_explanation(candidature)
    document = fitz.open(stream=octets, filetype="pdf")
    metadonnees = document.metadata
    document.close()

    assert not metadonnees.get("author")
    assert "Alice" not in metadonnees.get("title", "")


# --- Document ----------------------------------------------------------------
def test_the_filename_carries_the_date(candidature):
    nom = report_pdf.candidate_explanation_filename(candidature, dt.date(2026, 3, 14))
    assert nom.startswith("votre-candidature_")
    assert nom.endswith("_2026-03-14.pdf")


def test_every_page_is_numbered(candidature):
    octets = report_pdf.build_candidate_explanation(candidature)
    document = fitz.open(stream=octets, filetype="pdf")
    total = document.page_count
    for numero, page in enumerate(document, start=1):
        assert f"page {numero} / {total}" in page.get_text()
    document.close()


def test_two_builds_give_the_same_text(candidature):
    jour = dt.date(2026, 3, 14)
    premier = _texte(report_pdf.build_candidate_explanation(candidature, today=jour))
    second = _texte(report_pdf.build_candidate_explanation(candidature, today=jour))
    assert premier == second


# --- Vue ---------------------------------------------------------------------
def test_the_view_serves_a_pdf(client, candidature, recruteur):
    client.force_login(recruteur)
    reponse = client.get(
        reverse("candidates:export_candidate_explanation", kwargs={"pk": candidature.pk})
    )

    assert reponse.status_code == 200
    assert reponse["Content-Type"] == "application/pdf"
    assert "votre-candidature_" in reponse["Content-Disposition"]
    assert reponse.content.startswith(b"%PDF")


def test_the_export_is_journalised_with_its_own_scope(client, candidature, recruteur):
    """Un document parti vers un candidat n'a pas le meme sens qu'un dossier
    tire pour un entretien : le journal doit pouvoir les distinguer."""
    client.force_login(recruteur)
    client.get(
        reverse("candidates:export_candidate_explanation", kwargs={"pk": candidature.pk})
    )

    entree = AuditLog.objects.filter(action=AuditLog.Action.DATA_EXPORTED).latest(
        "created_at"
    )
    assert entree.metadata["scope"] == "explication_candidat"
    assert entree.object_id == str(candidature.pk)


def test_an_anonymous_caller_is_refused(client, candidature):
    reponse = client.get(
        reverse("candidates:export_candidate_explanation", kwargs={"pk": candidature.pk})
    )
    assert reponse.status_code == 302
    assert not AuditLog.objects.filter(action=AuditLog.Action.DATA_EXPORTED).exists()
