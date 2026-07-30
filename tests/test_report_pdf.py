"""Tests du rapport d'evaluation en PDF.

Un rapport de conformite se juge sur deux choses : les chiffres qu'il contient
et la provenance qui permet de les recouper six mois plus tard. Les tests
verifient les deux, et qu'aucune section ne disparait en silence lorsque la
pagination se declenche.
"""

from __future__ import annotations

import datetime as dt

import fitz
import pytest
from django.urls import reverse

from apps.core.models import AuditLog
from apps.evaluation import bias, harness, report_pdf, search_eval, threshold
from apps.matching import engine

DATASET = "ranking_v1"


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


@pytest.fixture
def observateur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="obs", password="mot-de-passe-de-test-123", role="viewer"
    )


@pytest.fixture
def sources(db):
    standard, _, mitigations = bias.compare_blind(DATASET)
    return report_pdf.Sources(
        quality=harness.run(DATASET),
        bias=standard,
        mitigations=mitigations,
        calibration=threshold.calibrate(DATASET),
        search=search_eval.run(),
    )


def _texte(octets: bytes) -> str:
    document = fitz.open(stream=octets, filetype="pdf")
    brut = "\n".join(page.get_text() for page in document)
    document.close()
    return report_pdf.texte_normalise(brut)


# --- Formatage ---------------------------------------------------------------
def test_numbers_use_the_french_separator():
    """L'interface ecrit « 0,997 » : un rapport en « 0.997 » ferait deux sources."""
    assert report_pdf.nombre(0.9971) == "0,997"
    assert report_pdf.nombre(1.0) == "1,000"
    assert report_pdf.nombre(0.85, 2) == "0,85"


def test_ligatures_are_undone():
    """Le texte extrait d'un PDF contient « Eﬀet », pas « Effet »."""
    assert report_pdf.texte_normalise("Eﬀet") == "Effet"
    assert report_pdf.texte_normalise("conﬁance") == "confiance"


# --- Document produit --------------------------------------------------------
def test_the_report_is_a_valid_pdf(sources):
    octets = report_pdf.build(sources, author="Nadia Cherkaoui")

    assert octets.startswith(b"%PDF")
    document = fitz.open(stream=octets, filetype="pdf")
    assert document.page_count >= 1
    document.close()


def test_every_section_is_present(sources):
    """La pagination ne doit pas escamoter une section."""
    texte = _texte(report_pdf.build(sources, author="Nadia Cherkaoui"))

    for titre in [
        "Rapport d'evaluation",
        "Qualite du classement",
        "Effet des attributs identitaires",
        "Seuil de tri recommande",
        "Qualite de la recherche",
        "Provenance",
    ]:
        assert titre in texte, f"section absente du PDF : {titre}"


def test_the_figures_match_the_sources(sources):
    texte = _texte(report_pdf.build(sources))

    attendu = report_pdf.nombre(sources.quality.aggregate["ndcg_at_5"])
    assert attendu in texte
    assert report_pdf.nombre(sources.calibration.recommended.precision) in texte


def test_the_report_carries_its_provenance(sources):
    """Sans version de moteur ni version de jeu, le document ne prouve rien."""
    texte = _texte(report_pdf.build(sources, author="Nadia Cherkaoui"))

    assert engine.ENGINE_VERSION in texte
    assert sources.quality.dataset_version in texte
    assert "Nadia Cherkaoui" in texte


def test_an_unidentified_export_says_so(sources):
    texte = _texte(report_pdf.build(sources, author=""))
    assert "compte non identifie" in texte


def test_the_date_appears_and_is_the_one_given(sources):
    jour = dt.date(2026, 3, 14)
    texte = _texte(report_pdf.build(sources, today=jour))
    assert "14/03/2026" in texte


def test_every_page_is_numbered(sources):
    octets = report_pdf.build(sources)
    document = fitz.open(stream=octets, filetype="pdf")
    total = document.page_count
    for numero, page in enumerate(document, start=1):
        assert f"page {numero} / {total}" in page.get_text()
    document.close()


def test_the_metadata_is_filled(sources):
    octets = report_pdf.build(sources, author="Nadia Cherkaoui")
    document = fitz.open(stream=octets, filetype="pdf")

    assert engine.ENGINE_VERSION in document.metadata["title"]
    assert document.metadata["author"] == "Nadia Cherkaoui"
    document.close()


def test_the_report_flags_a_narrow_margin(sources):
    """Un seuil parfait sur une marge d'un point doit etre presente comme tel."""
    texte = _texte(report_pdf.build(sources))
    if sources.calibration.perfectly_separable:
        assert "mefiance" in texte
        assert "marge" in texte


def test_the_report_states_what_the_threshold_does_not_do(sources):
    texte = _texte(report_pdf.build(sources))
    assert "n'ecarte aucune candidature" in texte


def test_the_search_section_is_skipped_when_absent(sources):
    sources.search = None
    texte = _texte(report_pdf.build(sources))

    assert "Qualite de la recherche" not in texte
    # Le reste du document doit rester complet.
    assert "Provenance" in texte
    assert "Seuil de tri recommande" in texte


def test_the_filename_carries_date_and_engine():
    nom = report_pdf.filename(dt.date(2026, 3, 14))
    assert nom == f"recrutement-ia_evaluation_2026-03-14_moteur-{engine.ENGINE_VERSION}.pdf"


def test_two_builds_give_the_same_text(sources):
    """Le rapport agrege des mesures deterministes : son contenu doit l'etre."""
    jour = dt.date(2026, 3, 14)
    premier = _texte(report_pdf.build(sources, author="X", today=jour))
    second = _texte(report_pdf.build(sources, author="X", today=jour))
    assert premier == second


# --- Vue d'export ------------------------------------------------------------
def test_the_export_view_serves_a_pdf(client, recruteur):
    client.force_login(recruteur)
    reponse = client.get(reverse("evaluation:export_pdf"))

    assert reponse.status_code == 200
    assert reponse["Content-Type"] == "application/pdf"
    assert "attachment" in reponse["Content-Disposition"]
    assert ".pdf" in reponse["Content-Disposition"]
    assert reponse.content.startswith(b"%PDF")


def test_the_export_is_journalised(client, recruteur):
    """Un document qui sort du systeme est une donnee qui circule."""
    client.force_login(recruteur)
    client.get(reverse("evaluation:export_pdf"))

    entree = AuditLog.objects.filter(action=AuditLog.Action.DATA_EXPORTED).latest(
        "created_at"
    )
    assert entree.actor == recruteur
    assert entree.metadata["format"] == "pdf"
    assert entree.metadata["engine_version"] == engine.ENGINE_VERSION
    assert "seuil" in entree.metadata["sections"]


def test_a_viewer_can_export(client, observateur):
    """Exporter ne modifie aucun dossier : le role lecteur y a droit."""
    client.force_login(observateur)
    reponse = client.get(reverse("evaluation:export_pdf"))

    assert reponse.status_code == 200
    assert AuditLog.objects.filter(action=AuditLog.Action.DATA_EXPORTED).exists()


def test_an_anonymous_caller_is_refused(client, db):
    reponse = client.get(reverse("evaluation:export_pdf"))
    assert reponse.status_code == 302
    assert not AuditLog.objects.filter(action=AuditLog.Action.DATA_EXPORTED).exists()


def test_the_export_names_its_author(client, recruteur):
    client.force_login(recruteur)
    texte = _texte(client.get(reverse("evaluation:export_pdf")).content)
    assert str(recruteur) in texte
