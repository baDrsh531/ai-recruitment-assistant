"""Tests de la couche reseau, contre un vrai serveur HTTP.

Le reste de la suite simule l'appel modele. Ici, le client parle pour de vrai
en HTTP a un serveur compatible OpenAI : c'est le seul endroit ou sont
exercees la reprise sur erreur, le decodage contraint, le repli sur
`guided_json` et l'envoi d'images. Le serveur est demarre en processus, aucun
serveur d'inference externe n'est requis.
"""

from __future__ import annotations

import pytest

from apps.ai.client import (
    InferenceClient,
    InferenceError,
    image_part,
    strict_schema,
    text_part,
)
from apps.ai.mock_server import MockConfig, MockInferenceServer
from apps.ai.models import AIInvocation

SCHEMA = {
    "type": "object",
    "properties": {
        "titre": {"type": "string"},
        "annees": {"type": "integer"},
        "competences": {
            "type": "array",
            "items": {"type": "object", "properties": {"nom": {"type": "string"}}},
        },
    },
}


def _client(server: MockInferenceServer, *, kind: str = "chat") -> InferenceClient:
    return InferenceClient(
        {
            "BASE_URL": server.base_url,
            "MODEL": server.config.models[0],
            "API_KEY": "not-needed",
            "TIMEOUT": 10,
        },
        kind=kind,
    )


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Reduit les temporisations de reprise.

    En production, l'attente entre deux tentatives laisse au serveur le temps
    de se remettre. Dans les tests, elle ne ferait qu'ajouter une minute et
    demie de sommeil sans rien verifier de plus : ce qui est teste, c'est que
    la reprise a lieu, pas sa duree.
    """
    from apps.ai import client as client_module

    monkeypatch.setattr(client_module, "BACKOFF_SECONDS", (0.01, 0.02))


@pytest.fixture
def server():
    with MockInferenceServer() as running:
        yield running


# --- Protocole --------------------------------------------------------------
def test_lists_models(server):
    assert _client(server).list_models() == server.config.models


def test_plain_completion(db, server):
    response = _client(server).chat(
        [{"role": "user", "content": "Resume ce profil."}],
        purpose="test",
    )
    assert response.text
    assert response.parsed is None
    assert response.total_tokens > 0
    assert response.latency_ms >= 0


def test_constrained_output_conforms_to_schema(db, server):
    response = _client(server).chat(
        [{"role": "user", "content": "Extrais les donnees."}],
        schema=SCHEMA,
        schema_name="profil",
        purpose="test",
    )
    assert isinstance(response.parsed, dict)
    # `strict_schema` rend toutes les proprietes obligatoires : le serveur les
    # renvoie donc toutes, et le client doit toutes les recevoir.
    assert set(response.parsed) == set(SCHEMA["properties"])
    assert isinstance(response.parsed["annees"], int)
    assert isinstance(response.parsed["competences"], list)
    assert all("nom" in item for item in response.parsed["competences"])


def test_vision_message_is_accepted(db, server):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    response = _client(server, kind="vision").chat(
        [
            {
                "role": "user",
                "content": [text_part("Lis cette page."), image_part(png)],
            }
        ],
        schema=SCHEMA,
        purpose="test_vision",
    )
    assert response.parsed is not None
    # Le cout des images est repercute dans la comptabilite des tokens.
    assert response.prompt_tokens > 500


def test_embeddings_endpoint(db, server, settings):
    import numpy as np

    from apps.ai import embeddings

    settings.LLM = {"BASE_URL": server.base_url, "API_KEY": "not-needed"}
    embedder = embeddings.ServerEmbedder("mock-bge-m3", 64)
    vectors = embedder.encode(["Python et Django", "React et TypeScript"])

    assert vectors.shape == (2, 64)
    # Les vecteurs doivent etre normalises : la similarite devient un produit scalaire.
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


# --- Robustesse -------------------------------------------------------------
def test_retries_on_server_errors(db):
    """Un serveur qui echoue une fois sur deux doit rester exploitable."""
    with MockInferenceServer(MockConfig(fail_rate=0.5, seed=7)) as server:
        client = _client(server)
        successes = 0
        for _ in range(6):
            try:
                client.chat([{"role": "user", "content": "test"}], purpose="test")
                successes += 1
            except InferenceError:
                pass
        assert successes >= 4, "la reprise devrait absorber la majorite des 503"


def test_falls_back_to_guided_json(db):
    """Certains serveurs ignorent `response_format` mais acceptent `guided_json`."""
    with MockInferenceServer(MockConfig(reject_response_format=True)) as server:
        response = _client(server).chat(
            [{"role": "user", "content": "Extrais."}],
            schema=SCHEMA,
            purpose="test_fallback",
        )
        assert response.parsed is not None
        assert set(response.parsed) == set(SCHEMA["properties"])


def test_unreachable_server_raises_after_retries(db):
    client = InferenceClient(
        {"BASE_URL": "http://127.0.0.1:1/v1", "MODEL": "x", "TIMEOUT": 1},
        kind="chat",
    )
    with pytest.raises(InferenceError) as excinfo:
        client.chat([{"role": "user", "content": "test"}], purpose="test")
    assert "injoignable" in str(excinfo.value)


def test_missing_base_url_is_reported_clearly():
    with pytest.raises(InferenceError) as excinfo:
        InferenceClient({"BASE_URL": "", "MODEL": ""}, kind="chat")
    assert "check_ai" in str(excinfo.value)


# --- Modeles a raisonnement -------------------------------------------------
# Reproduit le comportement mesure sur Qwen3.6-35B : la reflexion part dans
# `reasoning_content`, et un budget de tokens trop court rend un contenu vide.
def test_thinking_is_disabled_by_default(db):
    with MockInferenceServer(MockConfig(reasoning=True)) as server:
        response = _client(server).chat(
            [{"role": "user", "content": "test"}], max_tokens=512, purpose="test"
        )
        assert response.reasoning == ""
        assert response.text
        assert response.completion_tokens < 200


def test_thinking_can_be_enabled_and_is_captured(db):
    with MockInferenceServer(MockConfig(reasoning=True)) as server:
        response = _client(server).chat(
            [{"role": "user", "content": "test"}],
            max_tokens=2048, thinking=True, purpose="test",
        )
        assert response.reasoning
        assert response.text
        # Le raisonnement est facture : c'est tout l'enjeu du reglage.
        assert response.completion_tokens > 300


def test_truncated_response_is_reported_as_such(db):
    """Sans ce controle, l'erreur remontee parlait de JSON malforme."""
    with (
        MockInferenceServer(MockConfig(reasoning=True)) as server,
        pytest.raises(InferenceError) as excinfo,
    ):
            _client(server).chat(
                [{"role": "user", "content": "test"}],
                schema=SCHEMA, max_tokens=64, thinking=True, purpose="test",
            )
    message = str(excinfo.value)
    assert "tronquee" in message
    assert "max_tokens" in message
    assert "raisonnement" in message


def test_truncated_response_is_journalised(db):
    """Une analyse manquante doit laisser une trace expliquant pourquoi."""
    with (
        MockInferenceServer(MockConfig(reasoning=True)) as server,
        pytest.raises(InferenceError),
    ):
            _client(server).chat(
                [{"role": "user", "content": "test"}],
                max_tokens=64, thinking=True, purpose="tronque",
            )
    invocation = AIInvocation.objects.get(purpose="tronque")
    assert invocation.status == AIInvocation.Status.ERROR
    assert "tronquee" in invocation.error
    assert invocation.thinking is True


def test_malformed_json_is_journalised(db, server, monkeypatch):
    """Meme exigence pour une reponse qui ne respecte pas le schema."""
    from apps.ai import client as client_module

    def bad_read(self, data, *, max_tokens, hardened):
        data["choices"][0]["message"]["content"] = "ceci n'est pas du JSON"
        return original(self, data, max_tokens=max_tokens, hardened=hardened)

    original = client_module.InferenceClient._read
    monkeypatch.setattr(client_module.InferenceClient, "_read", bad_read)

    with pytest.raises(InferenceError):
        _client(server).chat(
            [{"role": "user", "content": "test"}], schema=SCHEMA, purpose="malforme"
        )
    invocation = AIInvocation.objects.get(purpose="malforme")
    assert invocation.status == AIInvocation.Status.ERROR
    assert "JSON" in invocation.error


def test_thinking_flag_is_journalised(db, server):
    _client(server).chat([{"role": "user", "content": "a"}], purpose="sans")
    _client(server).chat(
        [{"role": "user", "content": "b"}], thinking=True, purpose="avec"
    )
    assert AIInvocation.objects.get(purpose="sans").thinking is False
    assert AIInvocation.objects.get(purpose="avec").thinking is True


# --- Tracabilite ------------------------------------------------------------
def test_every_call_is_journalised(db, server):
    _client(server).chat(
        [{"role": "user", "content": "test"}],
        schema=SCHEMA,
        purpose="extraction",
        prompt_id="cv_extraction",
        prompt_version="1.0.0",
    )
    invocation = AIInvocation.objects.get(purpose="extraction")
    assert invocation.status == AIInvocation.Status.OK
    assert invocation.prompt_version == "1.0.0"
    assert invocation.model == server.config.models[0]
    assert invocation.input_hash
    assert invocation.total_tokens > 0


def test_failures_are_journalised(db):
    client = InferenceClient(
        {"BASE_URL": "http://127.0.0.1:1/v1", "MODEL": "x", "TIMEOUT": 1},
        kind="chat",
    )
    with pytest.raises(InferenceError):
        client.chat([{"role": "user", "content": "test"}], purpose="echec")

    invocation = AIInvocation.objects.get(purpose="echec")
    assert invocation.status == AIInvocation.Status.ERROR
    assert invocation.error


def test_identical_calls_share_an_input_hash(db, server):
    client = _client(server)
    messages = [{"role": "user", "content": "meme requete"}]
    client.chat(messages, purpose="hash", temperature=0.0)
    client.chat(messages, purpose="hash", temperature=0.0)

    hashes = set(AIInvocation.objects.filter(purpose="hash").values_list("input_hash", flat=True))
    assert len(hashes) == 1, "des appels identiques doivent etre reconnaissables"


def test_connection_is_reused_between_calls(db, server):
    """Non-regression : ouvrir une connexion neuve coutait ~1 s par appel."""
    import time

    client = _client(server)
    messages = [{"role": "user", "content": "test"}]

    client.chat(messages, purpose="chauffe")  # premiere connexion
    started = time.perf_counter()
    for _ in range(5):
        client.chat(messages, purpose="mesure")
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"5 appels locaux devraient rester sous 1 s, mesure {elapsed:.2f} s"


def test_clients_are_shared_across_calls(settings, server):
    """`chat_client()` doit rendre la meme instance, sinon le pool est perdu."""
    from apps.ai.client import chat_client

    settings.LLM = {
        "BASE_URL": server.base_url, "MODEL": "m", "API_KEY": "k", "TIMEOUT": 5,
    }
    assert chat_client() is chat_client()


def test_strict_schema_is_sent_to_the_server(db, server):
    """Le durcissement doit s'appliquer avant l'envoi, pas apres la reponse."""
    hardened = strict_schema(SCHEMA)
    assert hardened["required"] == ["titre", "annees", "competences"]

    response = _client(server).chat(
        [{"role": "user", "content": "test"}], schema=SCHEMA, purpose="test"
    )
    assert set(response.parsed) == set(hardened["required"])
