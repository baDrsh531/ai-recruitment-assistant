"""Tests de la generation de questions d'entretien.

Le banc d'essai d'inference remplace le serveur : ces tests tournent en
integration continue.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from apps.ai.mock_server import MockConfig, MockInferenceServer
from apps.candidates.models import Application, Candidate, CandidateSkill, Experience
from apps.jobs.models import JobOffer, JobSkill
from apps.matching import engine, interview
from apps.matching.models import InterviewQuestion


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    from apps.ai import client as client_module

    monkeypatch.setattr(client_module, "BACKOFF_SECONDS", (0.01, 0.02))


@pytest.fixture
def server(settings):
    with MockInferenceServer() as running:
        settings.LLM = {
            "BASE_URL": running.base_url,
            "MODEL": running.config.models[0],
            "API_KEY": "not-needed",
            "TIMEOUT": 10,
        }
        yield running


@pytest.fixture
def application(db):
    offer = JobOffer.objects.create(
        title="Ingenieur Backend Python",
        description="APIs Django",
        experience_min_years=3,
    )
    JobSkill.objects.create(offer=offer, name="Python", min_years=3)
    JobSkill.objects.create(offer=offer, name="Kubernetes", min_years=2)

    candidate = Candidate.objects.create(
        full_name="Badr Sahraoui",
        email="badr@example.com",
        headline="Ingenieur backend",
        total_experience_years=4,
    )
    CandidateSkill.objects.create(candidate=candidate, name="Python", years=4)
    Experience.objects.create(
        candidate=candidate,
        title="Ingenieur backend",
        company="Exemple SARL",
        description="Conception d'APIs REST avec Django.",
        start_date=dt.date(2022, 1, 1),
        end_date=dt.date(2026, 1, 1),
    )
    return Application.objects.create(candidate=candidate, offer=offer)


# --- Generation -------------------------------------------------------------
def test_questions_are_generated_and_persisted(application, server):
    result = engine.score(application.candidate, application.offer)
    questions = interview.generate(application, result, count=4)

    assert questions
    assert application.interview_questions.count() == len(questions)
    first = application.interview_questions.first()
    assert first.question
    assert first.prompt_id == "interview_questions"
    assert first.prompt_version
    assert first.model


def test_generation_replaces_the_previous_set(application, server):
    """Deux generations ne doivent pas laisser deux jeux melanges."""
    result = engine.score(application.candidate, application.offer)
    interview.generate(application, result, count=4)
    premier = set(application.interview_questions.values_list("pk", flat=True))

    interview.generate(application, result, count=4)
    second = set(application.interview_questions.values_list("pk", flat=True))

    assert premier & second == set()
    assert application.interview_questions.count() == len(second)


def test_positions_are_contiguous(application, server):
    result = engine.score(application.candidate, application.offer)
    interview.generate(application, result, count=5)
    positions = list(application.interview_questions.values_list("position", flat=True))
    assert positions == sorted(positions)
    assert positions == list(range(len(positions)))


def test_an_unknown_intent_falls_back(application, server, monkeypatch):
    """Le schema contraint l'enum, mais la persistance ne doit pas s'y fier."""
    assert interview._valid_intent("verification") == "verification"
    assert interview._valid_intent("exploration") == "exploration"
    assert interview._valid_intent("fantaisie") == InterviewQuestion.Intent.VERIFICATION
    assert interview._valid_intent(None) == InterviewQuestion.Intent.VERIFICATION


def test_generation_is_journalised(application, server):
    from apps.ai.models import AIInvocation

    result = engine.score(application.candidate, application.offer)
    interview.generate(application, result, count=4)

    invocation = AIInvocation.objects.get(purpose="interview_questions")
    assert invocation.status == AIInvocation.Status.OK
    assert invocation.prompt_version
    assert invocation.thinking is False


def test_failure_leaves_the_previous_set_intact(application, server, settings):
    """Un serveur qui tombe ne doit pas effacer les questions deja produites."""
    result = engine.score(application.candidate, application.offer)
    interview.generate(application, result, count=4)
    avant = application.interview_questions.count()
    assert avant > 0

    settings.LLM = {**settings.LLM, "BASE_URL": "http://127.0.0.1:1/v1"}
    from apps.ai.client import InferenceError

    with pytest.raises(InferenceError):
        interview.generate(application, result, count=4)

    assert application.interview_questions.count() == avant


# --- Contenu du prompt ------------------------------------------------------
def test_the_model_never_sees_the_raw_cv(application):
    """Comme pour l'analyse, le modele travaille sur des donnees structurees."""
    profil = interview._profile(application.candidate)
    assert "Ingenieur backend" in profil
    assert "Python" in profil
    # Le nom du candidat n'a rien a faire dans une question d'entretien.
    assert "Badr Sahraoui" not in profil


def test_gaps_are_passed_to_the_prompt(application):
    result = engine.score(application.candidate, application.offer)
    assert "Kubernetes" in [gap["skill"] for gap in result.gaps]


# --- Interface --------------------------------------------------------------
def test_generation_view_requires_login(client, application):
    url = reverse("matching:generate_questions", kwargs={"pk": application.pk})
    assert client.post(url).status_code == 302


def test_generation_view_creates_questions(client, application, server, django_user_model):
    user = django_user_model.objects.create_user(
        username="rh", password="mot-de-passe-de-test-123"
    )
    client.force_login(user)

    url = reverse("matching:generate_questions", kwargs={"pk": application.pk})
    response = client.post(url)

    assert response.status_code == 302
    assert application.interview_questions.exists()


def test_questions_appear_on_the_application_page(
    client, application, server, django_user_model
):
    user = django_user_model.objects.create_user(
        username="rh2", password="mot-de-passe-de-test-123"
    )
    client.force_login(user)

    result = engine.score(application.candidate, application.offer)
    interview.generate(application, result, count=3)

    response = client.get(application.get_absolute_url())
    assert response.status_code == 200
    assert len(response.context["questions"]) == 3
    assert "Questions d'entretien" in response.content.decode()


def test_generation_failure_is_reported_to_the_user(
    client, application, settings, django_user_model
):
    user = django_user_model.objects.create_user(
        username="rh3", password="mot-de-passe-de-test-123"
    )
    client.force_login(user)
    settings.LLM = {"BASE_URL": "http://127.0.0.1:1/v1", "MODEL": "x", "TIMEOUT": 1}

    url = reverse("matching:generate_questions", kwargs={"pk": application.pk})
    response = client.post(url, follow=True)

    assert response.status_code == 200
    messages = [str(m) for m in response.context["messages"]]
    assert any("impossible" in message for message in messages)


# --- Schema -----------------------------------------------------------------
def test_schema_constrains_the_intent():
    intents = interview.INTERVIEW_SCHEMA["properties"]["questions"]["items"][
        "properties"
    ]["intent"]["enum"]
    assert set(intents) == {choice for choice, _ in InterviewQuestion.Intent.choices}


def test_schema_requires_an_anchor_field():
    proprietes = interview.INTERVIEW_SCHEMA["properties"]["questions"]["items"][
        "properties"
    ]
    assert "cv_claim" in proprietes
    assert "expected_signals" in proprietes


def test_degraded_server_still_yields_questions(application, settings):
    """Un serveur qui echoue par intermittence ne doit pas bloquer la feature."""
    with MockInferenceServer(MockConfig(fail_rate=0.4, seed=3)) as running:
        settings.LLM = {
            "BASE_URL": running.base_url,
            "MODEL": running.config.models[0],
            "API_KEY": "k",
            "TIMEOUT": 10,
        }
        result = engine.score(application.candidate, application.offer)
        questions = interview.generate(application, result, count=4)
    assert questions
