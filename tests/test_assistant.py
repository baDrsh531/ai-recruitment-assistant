"""Tests de l'assistant de recherche.

Le filtrage est du code pur : il se teste sans aucun modele. Seule la
traduction de la question et la redaction passent par le banc d'essai
d'inference, qui tourne en processus.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.ai.mock_server import MockInferenceServer
from apps.assistant import filters as filtres_module
from apps.assistant.filters import FilterSet, apply
from apps.assistant.models import RecruiterQuery
from apps.assistant.service import ask
from apps.candidates.models import (
    Application,
    Candidate,
    CandidateLanguage,
    CandidateSkill,
)
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine
from apps.matching.services import score_application


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setattr(
        engine.SkillMatcher, "_precompute_semantic", lambda self, *args: None
    )
    from apps.ai import client as client_module

    monkeypatch.setattr(client_module, "BACKOFF_SECONDS", (0.01, 0.02))


@pytest.fixture
def offre(db):
    offer = JobOffer.objects.create(
        title="Backend Python", description="x", experience_min_years=2,
        status=JobOffer.Status.OPEN,
    )
    JobSkill.objects.create(offer=offer, name="Python", min_years=2)
    JobSkill.objects.create(offer=offer, name="Django", min_years=1)
    return offer


def _candidat(offre, nom, competences, *, annees=4.0, langues=(), education=5):
    candidate = Candidate.objects.create(
        full_name=nom, email=f"{nom.lower()}@example.com",
        total_experience_years=annees, highest_education=education,
        location="Casablanca",
    )
    for name in competences:
        CandidateSkill.objects.create(
            candidate=candidate, name=name, years=annees, last_used_year=2026
        )
    for langue in langues:
        CandidateLanguage.objects.create(
            candidate=candidate, language=langue, level="C1"
        )
    Application.objects.create(candidate=candidate, offer=offre)
    return candidate


@pytest.fixture
def population(offre):
    _candidat(offre, "Alice", ["Python", "Django"], annees=5, langues=["Francais", "Anglais"])
    _candidat(offre, "Bob", ["Python", "React"], annees=3, langues=["Francais"])
    _candidat(offre, "Carla", ["DRF"], annees=6, langues=["Anglais"])
    _candidat(offre, "Diane", ["Comptabilite"], annees=10, langues=["Francais"])
    for application in Application.objects.filter(offer=offre):
        score_application(application, with_explanation=False)
    return offre


# --- Filtrage : du code, pas un modele --------------------------------------
def test_all_skills_must_be_present(population):
    resultats = apply(population, FilterSet(skills_all=["Python", "Django"]))
    assert {item.candidate.full_name for item in resultats} == {"Alice", "Carla"}


def test_ontology_makes_drf_satisfy_django(population):
    """« DRF » couvre une demande de Django : l'ontologie est dirigee."""
    resultats = apply(population, FilterSet(skills_all=["Django"]))
    assert "Carla" in {item.candidate.full_name for item in resultats}


def test_exclusion_filter(population):
    """« Qui connait Django mais pas React »."""
    resultats = apply(
        population, FilterSet(skills_all=["Django"], skills_none=["React"])
    )
    noms = {item.candidate.full_name for item in resultats}
    assert "Bob" not in noms
    assert "Alice" in noms


def test_any_of_filter(population):
    resultats = apply(population, FilterSet(skills_any=["React", "Comptabilite"]))
    assert {item.candidate.full_name for item in resultats} == {"Bob", "Diane"}


def test_language_filter_requires_all(population):
    resultats = apply(population, FilterSet(languages=["Francais", "Anglais"]))
    assert {item.candidate.full_name for item in resultats} == {"Alice"}


def test_experience_and_education_filters(population):
    assert {i.candidate.full_name for i in apply(population, FilterSet(min_years=5))} == {
        "Alice", "Carla", "Diane",
    }
    peu_diplome = Candidate.objects.get(full_name="Bob")
    peu_diplome.highest_education = 3
    peu_diplome.save()
    noms = {i.candidate.full_name for i in apply(population, FilterSet(min_education=5))}
    assert "Bob" not in noms


def test_score_filter_uses_the_latest_score(population):
    resultats = apply(population, FilterSet(min_score=0.5))
    assert resultats
    assert all(item.score >= 0.5 for item in resultats)
    assert "Diane" not in {item.candidate.full_name for item in resultats}


def test_results_are_ordered_by_score(population):
    resultats = apply(population, FilterSet(skills_any=["Python", "DRF"]))
    scores = [item.score for item in resultats]
    assert scores == sorted(scores, reverse=True)


def test_limit_is_capped(population):
    resultats = apply(population, FilterSet(limit=999))
    assert len(resultats) <= filtres_module.MAX_LIMIT


def test_withdrawn_applications_are_excluded(population):
    application = Application.objects.get(candidate__full_name="Alice")
    application.stage = Application.Stage.WITHDRAWN
    application.save()
    noms = {i.candidate.full_name for i in apply(population, FilterSet(skills_all=["Python"]))}
    assert "Alice" not in noms


def test_filtering_is_deterministic(population):
    filtres = FilterSet(skills_all=["Python"])
    premier = [item.candidate.pk for item in apply(population, filtres)]
    second = [item.candidate.pk for item in apply(population, filtres)]
    assert premier == second


# --- Bornage de la sortie du modele -----------------------------------------
def test_payload_is_bounded():
    """La sortie du modele est contrainte, jamais reprise telle quelle."""
    filtres = FilterSet.from_payload(
        {
            "skills_all": [f"c{i}" for i in range(50)],
            "min_years": -5,
            "min_score": 42,
            "limit": 9999,
            "location": "x" * 200,
        }
    )
    assert len(filtres.skills_all) == 12
    assert filtres.min_years == 0.0
    assert filtres.min_score == 1.0
    assert filtres.limit == filtres_module.MAX_LIMIT
    assert len(filtres.location) <= 80


def test_languages_misplaced_in_skills_are_moved_back():
    """Regression : « qui parle francais et anglais ? » ne renvoyait personne.

    Le modele rangeait les langues parmi les competences, et le filtre
    cherchait alors des competences nommees « Francais » et « Anglais ». Le
    prompt le precise desormais, mais une consigne de prompt n'est pas une
    garantie : la correction est faite ici aussi.
    """
    filtres = FilterSet.from_payload(
        {"skills_all": ["Python", "Francais", "Anglais"], "languages": []}
    )
    assert filtres.skills_all == ["Python"]
    assert filtres.languages == ["Francais", "Anglais"]


def test_language_detection_ignores_case_and_accents():
    filtres = FilterSet.from_payload({"skills_any": ["FRANÇAIS", "english", "Django"]})
    assert filtres.skills_any == ["Django"]
    assert set(filtres.languages) == {"FRANÇAIS", "english"}


def test_languages_are_not_duplicated():
    filtres = FilterSet.from_payload(
        {"skills_all": ["Anglais"], "languages": ["Anglais"]}
    )
    assert filtres.languages == ["Anglais"]


def test_language_question_now_finds_people(population):
    """Le cas exact qui echouait, verifie de bout en bout sur la base."""
    filtres = FilterSet.from_payload({"skills_all": ["Francais", "Anglais"]})
    resultats = apply(population, filtres)
    assert {item.candidate.full_name for item in resultats} == {"Alice"}


def test_empty_payload_is_handled():
    filtres = FilterSet.from_payload({})
    assert filtres.is_empty
    assert filtres.summary() == []


def test_summary_is_readable():
    filtres = FilterSet(skills_all=["Python"], min_years=3, languages=["Anglais"])
    resume = " ; ".join(filtres.summary())
    assert "Python" in resume
    assert "3 an" in resume
    assert "Anglais" in resume


# --- Chaine complete, contre le banc d'essai ---------------------------------
@pytest.fixture
def serveur(settings):
    with MockInferenceServer() as running:
        settings.LLM = {
            "BASE_URL": running.base_url,
            "MODEL": running.config.models[0],
            "API_KEY": "not-needed",
            "TIMEOUT": 10,
        }
        yield running


def test_ask_records_the_question(population, serveur):
    requete = ask(population, "Qui maitrise Python ?")

    assert requete.pk is not None
    assert requete.question == "Qui maitrise Python ?"
    assert requete.filter_prompt_version
    assert requete.answer_prompt_version
    assert requete.latency_ms >= 0
    assert requete.matched_count == len(requete.matched_ids)


def test_ask_rejects_an_empty_question(population, serveur):
    with pytest.raises(ValueError):
        ask(population, "   ")


def test_the_model_never_selects_the_candidates(population, serveur, monkeypatch):
    """Le modele traduit ; c'est le code qui choisit. La liste doit le prouver."""
    from apps.assistant import service

    monkeypatch.setattr(
        service, "_traduire",
        lambda client, offer, question: (FilterSet(skills_all=["Django"]), "1.0.0"),
    )
    requete = ask(population, "peu importe la question")

    attendus = {
        str(item.candidate.pk)
        for item in apply(population, FilterSet(skills_all=["Django"]))
    }
    assert set(requete.matched_ids) == attendus


def test_the_answer_only_sees_the_selected_rows(population, serveur, monkeypatch):
    from apps.assistant import service

    vus = {}
    original = service._rediger

    def espion(client, offer, question, filtres, resultats):
        vus["noms"] = [item.candidate.full_name for item in resultats]
        return original(client, offer, question, filtres, resultats)

    monkeypatch.setattr(service, "_rediger", espion)
    monkeypatch.setattr(
        service, "_traduire",
        lambda client, offer, question: (FilterSet(skills_all=["Comptabilite"]), "1.0.0"),
    )
    ask(population, "qui fait de la comptabilite ?")

    assert vus["noms"] == ["Diane"]


def test_a_failing_server_keeps_the_list(population, settings, monkeypatch):
    """La liste vient du code : elle reste juste meme sans redaction."""
    from apps.assistant import service

    monkeypatch.setattr(
        service, "_traduire",
        lambda client, offer, question: (FilterSet(skills_all=["Python"]), "1.0.0"),
    )
    settings.LLM = {"BASE_URL": "http://127.0.0.1:1/v1", "MODEL": "x", "TIMEOUT": 1}

    requete = ask(population, "qui maitrise Python ?")
    assert requete.matched_count > 0
    assert "n'a pas pu etre produite" in requete.answer


def test_rejected_criteria_are_stored(population, serveur, monkeypatch):
    from apps.assistant import service

    monkeypatch.setattr(
        service, "_traduire",
        lambda client, offer, question: (
            FilterSet(skills_all=["Python"], rejected_criteria=["age"]),
            "1.0.0",
        ),
    )
    requete = ask(population, "les candidats de moins de 30 ans qui font du Python")

    assert requete.rejected_criteria == ["age"]
    assert requete.has_rejected_criteria


def test_question_is_journalised_in_the_audit_log(population, serveur):
    from apps.core.models import AuditLog

    ask(population, "Qui maitrise Python ?")
    entree = AuditLog.objects.filter(action=AuditLog.Action.CANDIDATE_VIEWED).latest(
        "created_at"
    )
    assert "assistant" in entree.summary.lower()
    assert "matched" in entree.metadata


# --- Interface ---------------------------------------------------------------
@pytest.fixture
def recruteur(db, django_user_model):
    return django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123"
    )


def test_assistant_page_requires_login(client, offre):
    url = reverse("assistant:offer", kwargs={"slug": offre.slug})
    assert client.get(url).status_code == 302


def test_assistant_page_renders(client, population, recruteur):
    client.force_login(recruteur)
    response = client.get(reverse("assistant:offer", kwargs={"slug": population.slug}))

    assert response.status_code == 200
    assert response.context["suggestions"]
    assert "Poser une question" in response.content.decode()


def test_asking_through_the_view(client, population, recruteur, serveur):
    client.force_login(recruteur)
    url = reverse("assistant:ask", kwargs={"slug": population.slug})

    response = client.post(url, {"question": "Qui maitrise Python ?"}, follow=True)

    assert response.status_code == 200
    assert RecruiterQuery.objects.count() == 1
    assert response.context["latest"].question == "Qui maitrise Python ?"


def test_empty_question_is_reported(client, population, recruteur, serveur):
    client.force_login(recruteur)
    response = client.post(
        reverse("assistant:ask", kwargs={"slug": population.slug}),
        {"question": "  "}, follow=True,
    )
    messages = [str(m) for m in response.context["messages"]]
    assert any("question" in message.lower() for message in messages)
    assert RecruiterQuery.objects.count() == 0


def test_history_can_be_cleared(client, population, recruteur, serveur):
    client.force_login(recruteur)
    ask(population, "Qui maitrise Python ?", actor=recruteur)
    assert RecruiterQuery.objects.count() == 1

    client.post(reverse("assistant:clear", kwargs={"slug": population.slug}))
    assert RecruiterQuery.objects.count() == 0


def test_matched_candidates_keep_the_result_order(client, population, recruteur, serveur):
    client.force_login(recruteur)
    ask(population, "Qui maitrise Python ?", actor=recruteur)

    response = client.get(reverse("assistant:offer", kwargs={"slug": population.slug}))
    affiches = [str(candidate.pk) for candidate in response.context["matched"]]
    assert affiches == response.context["latest"].matched_ids
