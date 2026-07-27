"""Tests du moteur de scoring.

Aucun appel modele : le moteur est entierement deterministe, c'est justement
ce qui le rend testable. Les embeddings sont neutralises pour que les tests
mesurent l'ontologie et l'arithmetique, pas la disponibilite d'un modele.
"""

from __future__ import annotations

import datetime as dt

import pytest

from apps.candidates.models import (
    Application,
    Candidate,
    CandidateLanguage,
    CandidateSkill,
    Certification,
    Experience,
)
from apps.jobs.models import (
    EducationLevel,
    JobLanguage,
    JobOffer,
    JobSkill,
    LanguageLevel,
)
from apps.matching import engine, ontology
from apps.matching.services import latest_scores, override_score, score_application


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    """Neutralise le rapprochement semantique pour isoler l'ontologie."""
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )


# --- Ontologie --------------------------------------------------------------
def test_aliases_are_resolved():
    assert ontology.normalize("DRF") == "django rest framework"
    assert ontology.normalize("  PostGres ") == "postgresql"
    assert ontology.normalize("Node") == "node.js"


def test_implication_is_transitive():
    assert "python" in ontology.closure("django")
    assert "python" in ontology.closure("drf")  # drf -> django -> python
    assert "api" in ontology.closure("drf")  # drf -> rest -> api


def test_relatedness_levels():
    assert ontology.relatedness("Python", "python") == 1.0
    assert ontology.relatedness("DRF", "Django REST Framework") == 1.0
    # Voisines mais distinctes.
    assert 0 < ontology.relatedness("PyTorch", "TensorFlow") < 1
    # Aucune relation connue.
    assert ontology.relatedness("Python", "Comptabilite") == 0.0


def test_implication_is_directional():
    """Django implique Python, jamais l'inverse.

    Rendre cette relation symetrique crediterait a un candidat des
    competences qu'il n'a pas — la faute la plus grave possible ici.
    """
    # Offre : Python. Candidat : Django. Il sait forcement Python.
    assert ontology.relatedness("Python", "Django") == ontology.IMPLICATION_SCORE
    # Offre : Django. Candidat : Python. Il a le prerequis, pas la competence.
    assert ontology.relatedness("Django", "Python") == ontology.PREREQUISITE_SCORE
    assert ontology.PREREQUISITE_SCORE < ontology.IMPLICATION_SCORE


# --- Fixtures metier --------------------------------------------------------
@pytest.fixture
def python_offer(db):
    offer = JobOffer.objects.create(
        title="Ingenieur Backend Python",
        description="APIs Django",
        location="Casablanca",
        remote_policy=JobOffer.RemotePolicy.ONSITE,
        experience_min_years=3,
        education_level=EducationLevel.BACHELOR,
        status=JobOffer.Status.OPEN,
    )
    JobSkill.objects.create(offer=offer, name="Python", weight=2.0, min_years=3)
    JobSkill.objects.create(offer=offer, name="Django", weight=1.5, min_years=2)
    JobSkill.objects.create(
        offer=offer, name="Kubernetes", weight=1.0,
        requirement=JobSkill.Requirement.PREFERRED,
    )
    JobLanguage.objects.create(offer=offer, language="Francais", min_level=LanguageLevel.C1)
    JobLanguage.objects.create(offer=offer, language="Anglais", min_level=LanguageLevel.B2)
    return offer


def make_candidate(db, name="Candidat", *, skills=(), years=0.0, education=0, location=""):
    candidate = Candidate.objects.create(
        full_name=name,
        email=f"{name.lower().replace(' ', '.')}@example.com",
        total_experience_years=years,
        highest_education=education,
        location=location,
    )
    for skill_name, skill_years in skills:
        CandidateSkill.objects.create(
            candidate=candidate, name=skill_name, years=skill_years, last_used_year=2026
        )
    return candidate


# --- Determinisme -----------------------------------------------------------
def test_score_is_reproducible(db, python_offer):
    candidate = make_candidate(db, "Ahmed", skills=[("Python", 5), ("Django", 4)], years=5)
    first = engine.score(candidate, python_offer)
    second = engine.score(candidate, python_offer)
    assert first.overall == second.overall
    assert first.breakdown() == second.breakdown()


def test_engine_version_is_recorded(db, python_offer):
    candidate = make_candidate(db, "Ahmed", skills=[("Python", 5)], years=5)
    assert engine.score(candidate, python_offer).engine_version == engine.ENGINE_VERSION


# --- Correspondance de competences ------------------------------------------
def test_exact_match_scores_full(db, python_offer):
    candidate = make_candidate(db, "Exact", skills=[("Python", 5), ("Django", 4)], years=5)
    result = engine.score(candidate, python_offer)
    python_match = next(m for m in result.skill_matches if m.required == "Python")
    assert python_match.method == "exact"
    assert python_match.score == 1.0


def test_ontology_infers_python_from_django(db, python_offer):
    """Le candidat ne declare que Django : Python doit etre credite."""
    candidate = make_candidate(db, "Implicite", skills=[("Django", 4)], years=4)
    result = engine.score(candidate, python_offer)
    python_match = next(m for m in result.skill_matches if m.required == "Python")
    assert python_match.method == "ontologie"
    assert python_match.matched_with == "Django"
    assert python_match.score >= 0.7


def test_alias_is_matched(db):
    offer = JobOffer.objects.create(title="API", description="x")
    JobSkill.objects.create(offer=offer, name="Django REST Framework")
    candidate = make_candidate(db, "Alias", skills=[("DRF", 3)])
    result = engine.score(candidate, offer)
    assert result.skill_matches[0].method == "exact"
    assert result.skill_matches[0].score == 1.0


def test_unrelated_skills_score_zero(db, python_offer):
    candidate = make_candidate(db, "Hors sujet", skills=[("Comptabilite", 10)], years=10)
    result = engine.score(candidate, python_offer)
    assert all(m.score == 0.0 for m in result.skill_matches)


def test_insufficient_seniority_is_penalised_not_zeroed(db, python_offer):
    """Un an sur trois demandes doit reduire le score, pas l'annuler."""
    junior = make_candidate(db, "Junior", skills=[("Python", 1)], years=1)
    senior = make_candidate(db, "Senior", skills=[("Python", 5)], years=5)

    junior_match = next(
        m for m in engine.score(junior, python_offer).skill_matches if m.required == "Python"
    )
    senior_match = next(
        m for m in engine.score(senior, python_offer).skill_matches if m.required == "Python"
    )
    assert 0.7 <= junior_match.score < senior_match.score == 1.0


def test_stale_skill_scores_lower(db, python_offer):
    recent = make_candidate(db, "Recent", skills=[("Python", 5)], years=5)
    old = make_candidate(db, "Ancien", skills=[("Python", 5)], years=5)
    old.skills.update(last_used_year=2015)

    recent_score = engine.score(recent, python_offer).criterion("skills").score
    old_score = engine.score(old, python_offer).criterion("skills").score
    assert old_score < recent_score


# --- Criteres ---------------------------------------------------------------
def test_experience_curve_is_soft(db, python_offer):
    half = make_candidate(db, "Moitie", skills=[("Python", 2)], years=1.5)
    criterion = engine.score(half, python_offer).criterion("experience")
    # 50 % de l'anciennete demandee -> environ 0.62, pas 0.50.
    assert 0.58 < criterion.score < 0.68


def test_overqualification_is_not_penalised(db, python_offer):
    """Penaliser la surqualification correlerait avec l'age : critere exclu."""
    expected = make_candidate(db, "Pile", skills=[("Python", 3)], years=3)
    senior = make_candidate(db, "Tres senior", skills=[("Python", 20)], years=20)
    assert (
        engine.score(senior, python_offer).criterion("experience").score
        == engine.score(expected, python_offer).criterion("experience").score
        == 1.0
    )


def test_languages_penalise_each_missing_level(db, python_offer):
    candidate = make_candidate(db, "Langues", skills=[("Python", 5)], years=5)
    CandidateLanguage.objects.create(
        candidate=candidate, language="Francais", level=LanguageLevel.C1
    )
    CandidateLanguage.objects.create(
        candidate=candidate, language="Anglais", level=LanguageLevel.A2
    )  # B2 demande, A2 obtenu : deux paliers manquants
    criterion = engine.score(candidate, python_offer).criterion("languages")
    assert criterion.applicable
    assert criterion.score == pytest.approx((1.0 + 0.5) / 2)


def test_missing_language_scores_zero(db, python_offer):
    candidate = make_candidate(db, "Muet", skills=[("Python", 5)], years=5)
    CandidateLanguage.objects.create(
        candidate=candidate, language="Francais", level=LanguageLevel.C2
    )
    criterion = engine.score(candidate, python_offer).criterion("languages")
    assert criterion.score == pytest.approx(0.5)  # anglais absent


def test_education_partial_credit(db, python_offer):
    candidate = make_candidate(
        db, "Bac", skills=[("Python", 5)], years=5, education=EducationLevel.HIGH_SCHOOL
    )
    criterion = engine.score(candidate, python_offer).criterion("education")
    assert criterion.score == pytest.approx(1 / 3, abs=1e-3)


def test_location_matches_on_shared_token(db, python_offer):
    local = make_candidate(db, "Local", skills=[("Python", 5)], years=5, location="Casablanca")
    remote = make_candidate(db, "Loin", skills=[("Python", 5)], years=5, location="Tanger")
    assert engine.score(local, python_offer).criterion("location").score == 1.0
    assert engine.score(remote, python_offer).criterion("location").score < 0.5


# --- Renormalisation --------------------------------------------------------
def test_unstated_criteria_are_excluded_and_weights_renormalised(db):
    """Une offre muette sur les langues ne doit pas leur attribuer de poids."""
    offer = JobOffer.objects.create(title="Minimal", description="x")  # aucune exigence
    JobSkill.objects.create(offer=offer, name="Python")
    candidate = make_candidate(db, "Simple", skills=[("Python", 5)], years=5)

    result = engine.score(candidate, offer)
    names = {criterion.name for criterion in result.criteria if criterion.applicable}
    assert names == {"skills"}
    assert result.weights_used == {"skills": 1.0}
    assert result.overall == pytest.approx(1.0)


def test_applied_weights_sum_to_one(db, python_offer):
    candidate = make_candidate(
        db, "Complet", skills=[("Python", 5), ("Django", 4)], years=5,
        education=EducationLevel.MASTER, location="Casablanca",
    )
    CandidateLanguage.objects.create(
        candidate=candidate, language="Francais", level=LanguageLevel.NATIVE
    )
    result = engine.score(candidate, python_offer)
    assert sum(result.weights_used.values()) == pytest.approx(1.0, abs=1e-3)
    assert 0.0 <= result.overall <= 1.0


def test_certifications_excluded_when_offer_is_silent(db, python_offer):
    candidate = make_candidate(db, "Certifie", skills=[("Python", 5)], years=5)
    Certification.objects.create(candidate=candidate, name="AWS Solutions Architect")
    assert not engine.score(candidate, python_offer).criterion("certifications").applicable


def test_certifications_counted_when_required(db, python_offer):
    python_offer.required_certifications = ["AWS Solutions Architect", "CKA"]
    python_offer.save()
    candidate = make_candidate(db, "Certifie", skills=[("Python", 5)], years=5)
    Certification.objects.create(candidate=candidate, name="AWS Solutions Architect - Associate")

    criterion = engine.score(candidate, python_offer).criterion("certifications")
    assert criterion.applicable
    assert criterion.score == pytest.approx(0.5)  # 1 sur 2


# --- Ecarts et classement ---------------------------------------------------
def test_gaps_list_missing_required_skills(db, python_offer):
    """Un candidat Python sans Django doit voir Django signale comme ecart."""
    candidate = make_candidate(db, "Partiel", skills=[("Python", 5)], years=5)
    result = engine.score(candidate, python_offer)
    assert [gap["skill"] for gap in result.gaps] == ["Django"]
    # Kubernetes est souhaitee, pas obligatoire : ce n'est pas un ecart.
    assert "Kubernetes" not in [gap["skill"] for gap in result.gaps]

    # Le prerequis est tout de meme credite partiellement, pas ignore.
    django = next(m for m in result.skill_matches if m.required == "Django")
    assert django.matched_with == "Python"
    assert 0 < django.score < engine.GAP_THRESHOLD


def test_better_candidate_ranks_higher(db, python_offer):
    strong = make_candidate(
        db, "Fort", skills=[("Python", 6), ("Django", 5), ("Kubernetes", 2)],
        years=6, education=EducationLevel.MASTER, location="Casablanca",
    )
    weak = make_candidate(db, "Faible", skills=[("Python", 1)], years=1, location="Tanger")
    assert engine.score(strong, python_offer).overall > engine.score(weak, python_offer).overall


# --- Recevabilite -----------------------------------------------------------
def test_off_topic_candidate_cannot_score_high(db, python_offer):
    """Le defaut trouve par le harnais : un hors sujet bien situe montait a 53 %.

    Sans competence obligatoire couverte, un profil ne doit pas remonter parce
    qu'il habite la bonne ville, parle les bonnes langues et a le bon diplome.
    """
    accountant = make_candidate(
        db, "Comptable", skills=[("Comptabilite", 12)], years=12,
        education=EducationLevel.MASTER, location="Casablanca",
    )
    CandidateLanguage.objects.create(
        candidate=accountant, language="Francais", level=LanguageLevel.NATIVE
    )
    CandidateLanguage.objects.create(
        candidate=accountant, language="Anglais", level=LanguageLevel.C1
    )

    result = engine.score(accountant, python_offer)
    assert result.criterion("skills").score == 0.0
    # La moyenne ponderee reste elevee : ce sont bien les autres criteres.
    assert result.weighted_score > 0.4
    assert result.admissibility == pytest.approx(engine.ADMISSIBILITY_FLOOR)
    assert result.overall < 0.15


def test_admissibility_is_neutral_for_qualified_candidates(db, python_offer):
    """Le facteur ne peut que penaliser un hors sujet, jamais avantager."""
    candidate = make_candidate(
        db, "Qualifie", skills=[("Python", 5), ("Django", 4)], years=5
    )
    result = engine.score(candidate, python_offer)
    assert result.admissibility == 1.0
    assert result.overall == pytest.approx(result.weighted_score)


def test_admissibility_ramps_between_floor_and_one(db, python_offer):
    """Couverture partielle : penalite partielle, pas de seuil brutal."""
    partial = make_candidate(db, "Partiel", skills=[("Python", 5)], years=5)
    result = engine.score(partial, python_offer)
    assert engine.ADMISSIBILITY_FLOOR < result.admissibility <= 1.0


def test_admissibility_neutral_when_offer_lists_no_required_skill(db):
    offer = JobOffer.objects.create(title="Souhaits seuls", description="x")
    JobSkill.objects.create(
        offer=offer, name="Docker", requirement=JobSkill.Requirement.PREFERRED
    )
    candidate = make_candidate(db, "Sans Docker", skills=[("Python", 5)], years=5)
    assert engine.score(candidate, offer).admissibility == 1.0


def test_semantic_flag_reports_degraded_mode(db, python_offer):
    candidate = make_candidate(db, "Ahmed", skills=[("Python", 5)], years=5)
    assert engine.score(candidate, python_offer).semantic_used is False


# --- Rapprochement semantique : desactive apres mesure -----------------------
def test_semantic_matching_is_disabled_by_default(settings):
    """Decision documentee, pas oubli de configuration.

    Un modele de phrases generaliste n'a aucune connaissance technique : sur
    les paires de reference de `manage.py probe_semantic`, il note
    « Kubernetes / Boulangerie » au-dessus de « Symfony / Laravel ». Les paires
    proches et les paires sans rapport se chevauchent, aucun seuil ne les
    separe. Activee, la couche crediterait un boulanger sur du Kubernetes.
    """
    assert settings.EMBEDDING["PROVIDER"] == "none"


def test_disabled_provider_yields_no_embedder(settings):
    from apps.ai import embeddings

    settings.EMBEDDING = {**settings.EMBEDDING, "PROVIDER": "none"}
    embeddings.reset_availability()
    assert embeddings.get_embedder_or_none() is None


def test_scoring_still_works_without_semantic_matching(db, python_offer, monkeypatch):
    """L'ontologie doit suffire : la desactivation ne degrade pas le service."""
    monkeypatch.undo()  # retablit le vrai `_precompute_semantic`
    candidate = make_candidate(
        db, "Sans embeddings", skills=[("Django", 4), ("PostgreSQL", 3)], years=4
    )
    result = engine.score(candidate, python_offer)

    assert result.semantic_used is False
    # Python est credite via l'ontologie, sans aucun embedding.
    python_match = next(m for m in result.skill_matches if m.required == "Python")
    assert python_match.method == "ontologie"
    assert python_match.score > 0.7


# --- Persistance et service -------------------------------------------------
@pytest.fixture
def application(db, python_offer):
    candidate = make_candidate(
        db, "Badr", skills=[("Python", 4), ("Django", 3)], years=4,
        education=EducationLevel.MASTER, location="Casablanca",
    )
    Experience.objects.create(
        candidate=candidate, title="Backend",
        start_date=dt.date(2022, 1, 1), end_date=dt.date(2026, 1, 1),
    )
    CandidateLanguage.objects.create(
        candidate=candidate, language="Francais", level=LanguageLevel.NATIVE
    )
    CandidateLanguage.objects.create(
        candidate=candidate, language="Anglais", level=LanguageLevel.B2
    )
    return Application.objects.create(candidate=candidate, offer=python_offer)


def test_score_application_persists_breakdown(application):
    score = score_application(application, with_explanation=False)
    assert score.overall > 0.8
    assert score.engine_version == engine.ENGINE_VERSION
    assert score.breakdown["criteria"]
    assert score.skill_matches
    assert score.compute_ms >= 0
    assert score.explanation == ""


def test_scoring_writes_audit_entry(application):
    from apps.core.models import AuditLog

    score_application(application, with_explanation=False)
    entry = AuditLog.objects.get(action=AuditLog.Action.SCORE_COMPUTED)
    assert entry.metadata["engine_version"] == engine.ENGINE_VERSION
    assert "weights_used" in entry.metadata


def test_history_is_kept_and_latest_wins(application):
    first = score_application(application, with_explanation=False)
    application.candidate.skills.filter(normalized_name="python").update(years=10)
    second = score_application(application, with_explanation=False)

    assert application.scores.count() == 2
    latest = latest_scores(application.offer)
    assert len(latest) == 1
    assert latest[0].pk == second.pk
    assert first.pk != second.pk


def test_manual_override_wins_over_computed(application, django_user_model):
    recruiter = django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123"
    )
    score = score_application(application, with_explanation=False)
    computed = score.overall

    override_score(score, value=0.4, reason="Entretien decevant", actor=recruiter)
    score.refresh_from_db()

    assert score.overall == computed  # le calcul n'est pas efface
    assert score.effective_score == 0.4
    assert score.is_overridden
    assert score.overridden_by == recruiter


def test_explanation_failure_does_not_lose_the_score(application, monkeypatch):
    """Serveur d'inference injoignable : le score doit exister quand meme."""
    from apps.ai.client import InferenceError
    from apps.matching import explain as explain_module

    def boom(*args, **kwargs):
        raise InferenceError("injoignable")

    monkeypatch.setattr(explain_module, "chat_client", boom)
    score = score_application(application, with_explanation=True)

    assert score.pk is not None
    assert score.overall > 0
    assert score.explanation == ""
