"""Tests de l'audit de biais.

Deux roles : verifier que l'audit fonctionne, et servir de garde-fou. Si une
evolution du moteur introduisait une sensibilite au nom du candidat, ces tests
echoueraient avant la mise en production.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.candidates.models import Candidate, CandidateSkill
from apps.evaluation import bias
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


@pytest.fixture(scope="session")
def audit_report(django_db_setup, django_db_blocker):
    """L'audit est lance une seule fois : environ 400 scorings."""
    with django_db_blocker.unblock():
        yield bias.audit("ranking_v1")


# --- Resultats de l'audit ---------------------------------------------------
def test_audit_covers_every_dimension(audit_report):
    assert {item.dimension for item in audit_report.dimensions} == set(bias.DIMENSIONS)
    assert all(item.comparisons > 0 for item in audit_report.dimensions)


def test_name_has_strictly_no_effect(audit_report):
    """Propriete centrale : le moteur ne lit jamais le nom du candidat."""
    names = next(
        item for item in audit_report.dimensions if item.dimension == "prenom_et_nom"
    )
    assert names.max_abs_delta == 0.0
    assert names.rank_changes == 0
    assert names.impact_ratio == 1.0
    assert not names.influences_score


@pytest.mark.parametrize("dimension", ["annee_de_diplome", "etablissement"])
def test_age_and_school_proxies_have_no_effect(audit_report, dimension):
    """L'annee de diplome est un indicateur d'age, l'etablissement un marqueur social."""
    item = next(d for d in audit_report.dimensions if d.dimension == dimension)
    assert item.max_abs_delta == 0.0
    assert item.rank_changes == 0


def test_location_is_the_only_active_lever(audit_report):
    influential = [d.dimension for d in audit_report.dimensions if d.influences_score]
    assert influential == ["localisation"]


def test_every_dimension_passes_the_four_fifths_rule(audit_report):
    failures = audit_report.failures()
    assert not failures, "Ratio d'impact insuffisant : " + ", ".join(
        f"{item.dimension} = {item.impact_ratio}" for item in failures
    )


def test_non_discrimination_properties_hold(audit_report):
    broken = audit_report.broken_properties()
    assert not broken, "Propriete mise en defaut : " + ", ".join(
        check.name for check in broken
    )
    assert {check.name for check in audit_report.properties} == {
        "nom_sans_effet",
        "surqualification_non_penalisee",
    }


def test_report_is_serialisable(audit_report):
    import json

    restored = json.loads(json.dumps(audit_report.as_dict(), ensure_ascii=False))
    assert restored["impact_ratio_threshold"] == bias.IMPACT_RATIO_THRESHOLD
    assert len(restored["dimensions"]) == len(bias.DIMENSIONS)


def test_audit_leaves_no_data_behind(db):
    offers_before = JobOffer.objects.count()
    candidates_before = Candidate.objects.count()
    bias.audit("ranking_v1")
    assert JobOffer.objects.count() == offers_before
    assert Candidate.objects.count() == candidates_before


# --- Mecanique --------------------------------------------------------------
def test_rank_of_handles_ties():
    scores = {"a": 0.9, "b": 0.9, "c": 0.5}

    class Stub:
        def __init__(self, pk):
            self.pk = pk

    # Deux ex aequo prennent tous deux le rang 1.
    assert bias._rank_of(Stub("a"), scores) == 1
    assert bias._rank_of(Stub("b"), scores) == 1
    assert bias._rank_of(Stub("c"), scores) == 3


def test_impact_ratio_is_one_when_nobody_is_selected():
    accumulator = bias._Accumulator("test", [bias.Variant("x", lambda c: None)])
    accumulator.observed["x"] = 4
    assert accumulator.result().impact_ratio == 1.0


def test_renaming_a_candidate_never_changes_the_score(db):
    """Le meme controle, sur un cas construit a la main."""
    offer = JobOffer.objects.create(
        title="Backend", description="x", experience_min_years=2
    )
    JobSkill.objects.create(offer=offer, name="Python", min_years=2)

    candidate = Candidate.objects.create(full_name="Marc Dupont", total_experience_years=4)
    CandidateSkill.objects.create(candidate=candidate, name="Python", years=4)

    reference = engine.score(candidate, offer).overall
    for name in ("Fatima El Amrani", "Wei Chen", "Marie Dupont", ""):
        candidate.full_name = name
        assert engine.score(candidate, offer).overall == reference


# --- Page de transparence ---------------------------------------------------
# --- Attenuation par le screening a l'aveugle -------------------------------
@pytest.fixture(scope="session")
def blind_report(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        yield bias.audit("ranking_v1", blind=True)


def test_blind_mode_neutralises_location(blind_report):
    """L'attenuation doit supprimer l'effet, pas seulement le reduire."""
    location = blind_report.dimension("localisation")
    assert location.max_abs_delta == 0.0
    assert location.rank_changes == 0
    assert location.impact_ratio == 1.0
    assert not location.influences_score


def test_blind_mode_leaves_other_dimensions_untouched(audit_report, blind_report):
    for dimension in ("prenom_et_nom", "annee_de_diplome", "etablissement"):
        assert (
            blind_report.dimension(dimension).impact_ratio
            == audit_report.dimension(dimension).impact_ratio
        )


def test_blind_mode_keeps_non_discrimination_properties(blind_report):
    assert not blind_report.broken_properties()


def test_comparison_quantifies_the_gain(db):
    standard, blind, mitigations = bias.compare_blind("ranking_v1")

    assert standard.blind is False
    assert blind.blind is True

    location = next(item for item in mitigations if item.dimension == "localisation")
    assert location.ratio_standard < location.ratio_blind
    assert location.gain > 0
    assert location.neutralised
    assert location.rank_changes_standard > 0
    assert location.rank_changes_blind == 0

    # Les autres attributs n'ont rien a gagner : ils etaient deja neutres.
    for item in mitigations:
        if item.dimension != "localisation":
            assert item.gain == 0.0
            assert not item.neutralised


# --- Effet du mode aveugle sur le moteur ------------------------------------
def test_blind_mode_excludes_location_and_renormalises(db):
    offer = JobOffer.objects.create(
        title="Sur site", description="x", location="Casablanca",
        remote_policy=JobOffer.RemotePolicy.ONSITE, experience_min_years=2,
    )
    JobSkill.objects.create(offer=offer, name="Python", min_years=2)
    candidate = Candidate.objects.create(
        full_name="Loin", total_experience_years=4, location="Tanger"
    )
    CandidateSkill.objects.create(candidate=candidate, name="Python", years=4)

    standard = engine.score(candidate, offer, blind=False)
    blind = engine.score(candidate, offer, blind=True)

    assert standard.criterion("location").applicable
    assert not blind.criterion("location").applicable
    assert "aveugle" in blind.criterion("location").detail["reason"]
    assert "location" not in blind.weights_used
    assert sum(blind.weights_used.values()) == pytest.approx(1.0, abs=1e-3)
    # Le candidat eloigne n'est plus penalise.
    assert blind.overall > standard.overall


def test_offer_policy_is_the_default(db):
    offer = JobOffer.objects.create(
        title="Aveugle", description="x", location="Casablanca",
        remote_policy=JobOffer.RemotePolicy.ONSITE, blind_screening=True,
    )
    JobSkill.objects.create(offer=offer, name="Python")
    candidate = Candidate.objects.create(full_name="X", location="Tanger")
    CandidateSkill.objects.create(candidate=candidate, name="Python", years=3)

    result = engine.score(candidate, offer)
    assert result.blind is True
    assert not result.criterion("location").applicable


def test_blind_flag_is_persisted_on_the_score(db):
    from apps.candidates.models import Application
    from apps.matching.services import score_application

    offer = JobOffer.objects.create(title="Aveugle", description="x", blind_screening=True)
    JobSkill.objects.create(offer=offer, name="Python")
    candidate = Candidate.objects.create(full_name="X")
    CandidateSkill.objects.create(candidate=candidate, name="Python", years=3)
    application = Application.objects.create(candidate=candidate, offer=offer)

    score = score_application(application, with_explanation=False)
    assert score.blind is True
    assert score.breakdown["blind"] is True


def test_blind_mode_masks_employers_in_the_prompt(db):
    """Un nom d'entreprise renseigne sur le milieu, pas sur la competence."""
    import datetime as dt

    from apps.candidates.models import Experience
    from apps.matching.explain import _candidate_summary

    candidate = Candidate.objects.create(full_name="X", highest_education=5)
    Experience.objects.create(
        candidate=candidate, title="Ingenieur", company="Cabinet Prestigieux SA",
        start_date=dt.date(2020, 1, 1), end_date=dt.date(2024, 1, 1),
    )

    assert "Cabinet Prestigieux SA" in _candidate_summary(candidate, blind=False)
    masked = _candidate_summary(candidate, blind=True)
    assert "Cabinet Prestigieux SA" not in masked
    assert "Entreprise A" in masked
    # Le poste, lui, reste : c'est une information de competence.
    assert "Ingenieur" in masked


def test_bias_page_requires_login(client, db):
    response = client.get(reverse("evaluation:bias_report"))
    assert response.status_code == 302


def test_bias_page_renders(client, db, django_user_model):
    user = django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123"
    )
    client.force_login(user)
    response = client.get(reverse("evaluation:bias_report"))

    assert response.status_code == 200
    assert response.context["report"].dimensions
    assert response.context["quality"].aggregate["ndcg_at_5"] > 0
    content = response.content.decode()
    # Assertions sur des jetons insecables : le texte courant du gabarit est
    # reparti sur plusieurs lignes et ne se prete pas a une recherche exacte.
    assert "prenom_et_nom" in content
    assert "localisation" in content
    assert "LL144" in content
    assert "nom_sans_effet" in content
