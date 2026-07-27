"""Tests de la fondation : modeles, couche IA, vues."""

import numpy as np
import pytest
from django.urls import reverse

from apps.ai import embeddings
from apps.ai.client import strict_schema
from apps.ai.prompts import REGISTRY, get
from apps.core.models import AuditLog
from apps.core.services import record_audit
from apps.jobs.models import DEFAULT_WEIGHTS, JobOffer


# --- Offres ----------------------------------------------------------------
def test_weights_are_normalised(offer):
    assert sum(offer.weights.values()) == pytest.approx(1.0)


def test_custom_weights_override_defaults(offer):
    offer.scoring_weights = {"skills": 0.9}
    assert offer.weights["skills"] > DEFAULT_WEIGHTS["skills"]
    assert sum(offer.weights.values()) == pytest.approx(1.0)


def test_slug_is_unique(db):
    first = JobOffer.objects.create(title="Data Engineer", description="x")
    second = JobOffer.objects.create(title="Data Engineer", description="y")
    assert first.slug != second.slug


def test_skill_normalisation(offer):
    skill = offer.skills.get(name="Python")
    assert skill.normalized_name == "python"


# --- Audit -----------------------------------------------------------------
def test_audit_log_is_immutable(db, recruiter, offer):
    entry = record_audit(
        AuditLog.Action.SCORE_COMPUTED, actor=recruiter, obj=offer, summary="test", score=91
    )
    assert entry.metadata["score"] == 91

    entry.summary = "modifie"
    with pytest.raises(ValueError):
        entry.save()
    with pytest.raises(ValueError):
        entry.delete()


# --- Couche IA -------------------------------------------------------------
def test_strict_schema_hardens_objects():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "skills": {
                "type": "array",
                "items": {"type": "object", "properties": {"label": {"type": "string"}}},
            },
        },
    }
    hardened = strict_schema(schema)
    assert hardened["additionalProperties"] is False
    assert hardened["required"] == ["name", "skills"]
    nested = hardened["properties"]["skills"]["items"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["label"]
    # L'original n'est pas modifie.
    assert "additionalProperties" not in schema


def test_every_prompt_is_versioned():
    assert REGISTRY
    for prompt_id, prompt in REGISTRY.items():
        assert prompt.id == prompt_id
        assert prompt.version.count(".") == 2, f"{prompt_id} : version semantique attendue"
        assert prompt.system.strip()


def test_prompt_rendering():
    messages = get("score_explanation").render(
        job_title="Backend", required_skills="Python", preferred_skills="Go",
        candidate_summary="…", score_breakdown="…",
    )
    assert messages[0]["role"] == "system"
    assert "Backend" in messages[1]["content"]


def test_unknown_prompt_raises():
    with pytest.raises(KeyError):
        get("prompt_inexistant")


# --- Vecteurs --------------------------------------------------------------
def test_pack_unpack_roundtrip():
    vector = np.array([0.1, -0.2, 0.35], dtype=np.float32)
    assert np.allclose(embeddings.unpack(embeddings.pack(vector)), vector)


def test_unpack_handles_empty():
    assert embeddings.unpack(None) is None
    assert embeddings.unpack(b"") is None


def test_top_k_orders_by_similarity():
    query = np.array([1.0, 0.0], dtype=np.float32)
    matrix = np.array([[0.0, 1.0], [1.0, 0.0], [0.7, 0.7]], dtype=np.float32)
    results = embeddings.top_k(query, matrix, k=3)
    assert [index for index, _ in results] == [1, 2, 0]
    assert results[0][1] == pytest.approx(1.0)


def test_stack_skips_missing_vectors():
    vectors = [np.array([1.0, 0.0], dtype=np.float32), None, np.array([0.0, 1.0], dtype=np.float32)]
    matrix, positions = embeddings.stack(vectors)
    assert matrix.shape == (2, 2)
    assert positions == [0, 2]


# --- Vues ------------------------------------------------------------------
@pytest.mark.parametrize(
    "url_name", ["candidates:dashboard", "candidates:list", "jobs:list"]
)
def test_views_require_login(client, db, url_name):
    response = client.get(reverse(url_name))
    assert response.status_code == 302
    assert "connexion" in response["Location"]


def test_dashboard_renders(client, recruiter, offer):
    client.force_login(recruiter)
    response = client.get(reverse("candidates:dashboard"))
    assert response.status_code == 200
    assert response.context["stats"]["open_offers"] == 1


def test_offer_detail_renders(client, recruiter, offer):
    client.force_login(recruiter)
    response = client.get(offer.get_absolute_url())
    assert response.status_code == 200
    assert "Ingenieur Backend Python" in response.content.decode()


def test_pages_render_with_debug_enabled(client, recruiter, offer, settings):
    """Regression : la barre de debogage doit avoir ses URLs enregistrees.

    La suite tourne avec DEBUG=False, ce qui court-circuite le middleware de
    django-debug-toolbar. Sans ce test, une page cassee uniquement en
    developpement passerait inapercue.
    """
    settings.DEBUG = True
    client.force_login(recruiter)
    response = client.get(reverse("candidates:dashboard"), REMOTE_ADDR="127.0.0.1")
    assert response.status_code == 200


def test_blind_screening_masks_name(db):
    from apps.candidates.models import Candidate

    candidate = Candidate.objects.create(full_name="Badr Sahraoui")
    assert candidate.display_name() == "Badr Sahraoui"
    assert candidate.display_name(blind=True).startswith("Candidat ")
    assert "Badr" not in candidate.display_name(blind=True)
