"""Tests du harnais d'evaluation de l'extraction.

Le harnais complet appelle le modele : il est marque `llm` et exclu de
l'integration continue. Tout ce qui l'entoure — fabrique de CV, comparaisons,
agregation, seuils — est teste ici sans aucun serveur d'inference.
"""

from __future__ import annotations

import datetime as dt

import fitz
import pytest

from apps.evaluation import cv_factory, extraction
from apps.parsing import extractors, pipeline, quality

PROFILE = {
    "full_name": "Karim Benjelloun",
    "headline": "Ingenieur logiciel backend",
    "email": "karim.benjelloun@example.com",
    "phone": "+212 661 234 567",
    "location": "Casablanca",
    "linkedin": "linkedin.com/in/karimbenjelloun",
    "github": "github.com/kbenjelloun",
    "skills": ["Python", "Django", "PostgreSQL", "Docker"],
    "languages": [
        {"language": "Francais", "level": "NAT"},
        {"language": "Anglais", "level": "C1"},
    ],
    "experiences": [
        {
            "title": "Ingenieur backend",
            "company": "Atlas Software",
            "location": "Casablanca",
            "start": "2022-03",
            "end": "",
            "description": "APIs REST avec Django.",
        }
    ],
    "education": [
        {"degree": "Master en genie logiciel", "institution": "Universite Hassan II",
         "year": 2019, "level": 5}
    ],
}


# --- Fabrique de CV ---------------------------------------------------------
@pytest.mark.parametrize("layout", cv_factory.LAYOUTS)
def test_every_layout_produces_a_readable_pdf(layout):
    pdf = cv_factory.build(PROFILE, layout)
    assert pdf.startswith(b"%PDF")
    with fitz.open(stream=pdf, filetype="pdf") as document:
        assert document.page_count == 1


def test_unknown_layout_is_rejected():
    with pytest.raises(ValueError, match="Mise en page inconnue"):
        cv_factory.build(PROFILE, "origami")


@pytest.mark.parametrize("layout", ["simple", "deux_colonnes", "tableau"])
def test_text_layouts_keep_the_identity_readable(layout):
    """Regression : l'en-tete du gabarit « tableau » etait trop court.

    PyMuPDF n'ecrit rien plutot que de deborder : le CV sortait sans nom ni
    contact, et l'identite extraite etait entierement fausse.
    """
    extracted = extractors.extract(cv_factory.build(PROFILE, layout), "cv.pdf")
    texte = extracted.full_text
    assert PROFILE["full_name"] in texte
    assert PROFILE["email"] in texte


def test_two_column_layout_is_detected_as_such():
    report = quality.assess(
        extractors.extract(cv_factory.build(PROFILE, "deux_colonnes"), "cv.pdf")
    )
    assert report.is_multi_column
    assert report.needs_vision


def test_scanned_layout_has_no_text_layer():
    """La voie vision doit etre la seule possible sur un document aplati."""
    report = quality.assess(
        extractors.extract(cv_factory.build(PROFILE, "scanne"), "cv.pdf")
    )
    assert not report.has_text_layer
    assert report.looks_scanned
    assert report.needs_vision


def test_render_dpi_is_high_enough_for_vision():
    """Regression : a 150 dpi, le modele vision ne lisait que les titres."""
    assert extractors.RENDER_DPI >= 200


# --- Analyse des dates ------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2021-06", dt.date(2021, 6, 1)),
        ("06/2021", dt.date(2021, 6, 1)),
        ("6/2021", dt.date(2021, 6, 1)),
        ("06-2021", dt.date(2021, 6, 1)),
        ("06.2021", dt.date(2021, 6, 1)),
        ("2021", dt.date(2021, 1, 1)),
        ("juin 2021", dt.date(2021, 6, 1)),
        ("Juin 2021", dt.date(2021, 6, 1)),
        ("June 2021", dt.date(2021, 6, 1)),
        ("septembre 2019", dt.date(2019, 9, 1)),
        ("aout 2020", dt.date(2020, 8, 1)),
        ("août 2020", dt.date(2020, 8, 1)),
        ("13/2021", None),
        ("present", None),
        ("", None),
        (None, None),
    ],
)
def test_dates_accept_the_formats_seen_on_real_cvs(value, expected):
    """Regression : seul AAAA-MM etait accepte.

    Le modele vision rendait « 06/2021 » en recopiant la forme lue sur le
    document : la date etait rejetee, l'experience enregistree sans periode, et
    l'anciennete totale tombait a zero sur trois cas d'evaluation sur cinq.
    """
    assert pipeline._parse_month(value) == expected


# --- Comparaisons du harnais ------------------------------------------------
def test_identity_comparison_ignores_case_and_accents():
    assert extraction._same_text("KARIM BENJELLOUN", "Karim Benjelloun")
    assert extraction._same_text("Fes", "Fès")
    assert not extraction._same_text("Rabat", "Casablanca")


def test_headline_comparison_is_tolerant():
    assert extraction._loose_text("Ingenieur backend", "Ingenieur backend senior")
    assert not extraction._loose_text("Comptable", "Ingenieur backend")


def test_phone_comparison_ignores_formatting():
    assert extraction._digits("+212 661 234 567") == extraction._digits("+212661234567")


# --- Jeu de donnees ---------------------------------------------------------
def test_dataset_is_well_formed():
    dataset = extraction.load_dataset("extraction_v1")
    identifiers = [case["id"] for case in dataset["cases"]]
    assert len(identifiers) == len(set(identifiers))

    layouts = set()
    for case in dataset["cases"]:
        profile = case["profile"]
        assert case["layout"] in cv_factory.LAYOUTS
        layouts.add(case["layout"])
        for champ in ("full_name", "email", "headline", "skills", "experiences"):
            assert profile.get(champ), f"{case['id']} : {champ} manquant"
        assert case["expected_years"] > 0

    # Le jeu doit couvrir la voie texte comme la voie vision.
    assert "simple" in layouts
    assert "scanne" in layouts


def test_unknown_dataset_raises():
    with pytest.raises(FileNotFoundError):
        extraction.load_dataset("inexistant")


# --- Agregation et seuils ---------------------------------------------------
def _case(**kwargs) -> extraction.CaseResult:
    base = {
        "id": "x", "layout": "simple", "method": "text", "seconds": 1.0,
        "identity_accuracy": 1.0,
        "skills": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
        "languages": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
        "evidence_total": 10, "evidence_anchored": 10,
    }
    return extraction.CaseResult(**{**base, **kwargs})


def test_unverifiable_cases_are_excluded_from_the_evidence_metric():
    """Un CV scanne n'a pas de couche texte : l'ancrage y est impossible.

    L'inclure ferait passer une limite connue du format pour un defaut du
    systeme, et ferait chuter la mesure sans que rien ne soit casse.
    """
    scanned = _case(
        id="scanne", layout="scanne", evidence_total=5, evidence_anchored=0,
        evidence_verifiable=False,
    )
    report = extraction.Report("t", "1", {}, [_case(), scanned])

    verifiables = [c for c in report.cases if c.evidence_verifiable]
    assert len(verifiables) == 1
    assert scanned.evidence_ratio == 0.0
    assert _case().evidence_ratio == 1.0


def test_thresholds_direction_is_respected():
    """L'erreur d'anciennete est la seule metrique ou plus bas vaut mieux."""
    assert {"experience_years_mae"} == extraction.LOWER_IS_BETTER

    good = extraction.Report("t", "1", {
        "identity_accuracy": 1.0, "skills_f1": 1.0, "languages_f1": 1.0,
        "evidence_anchored": 1.0, "experience_years_mae": 0.2,
    }, [])
    assert not good.failures()

    bad = extraction.Report("t", "1", {
        "identity_accuracy": 1.0, "skills_f1": 1.0, "languages_f1": 1.0,
        "evidence_anchored": 1.0, "experience_years_mae": 5.0,
    }, [])
    assert "experience_years_mae" in bad.failures()


def test_report_is_serialisable():
    import json

    report = extraction.Report("t", "1", {"skills_f1": 0.9}, [_case()])
    restored = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
    assert restored["thresholds"] == extraction.THRESHOLDS
    assert restored["cases"][0]["id"] == "x"


# --- Execution complete -----------------------------------------------------
@pytest.mark.llm
def test_full_extraction_run_meets_thresholds(db):
    """Necessite un serveur d'inference joignable. Exclu de la CI."""
    report = extraction.run("extraction_v1")
    failures = report.failures()
    assert not failures, "Seuils franchis : " + ", ".join(
        f"{name} = {value:.3f} (seuil {threshold})"
        for name, (value, threshold) in failures.items()
    )
