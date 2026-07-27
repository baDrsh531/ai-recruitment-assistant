"""Serveur d'inference factice, compatible OpenAI.

**Ce n'est pas un modele.** C'est un banc d'essai : il repond au protocole que
parle `apps/ai/client.py` — `/v1/models`, `/v1/chat/completions`,
`/v1/embeddings` — avec des donnees synthetiques conformes au schema demande.

A quoi il sert. Le reste du projet est teste avec l'appel modele simule ; la
couche reseau elle-meme ne l'etait pas. Ce serveur permet d'exercer pour de
vrai le client HTTP : decodage contraint par JSON Schema, repli sur
`guided_json`, reprise sur erreur, envoi d'images en base64, journalisation des
appels. Il rend le pipeline verifiable sans serveur d'inference joignable.

A quoi il ne sert pas. Les reponses sont fabriquees par des regles, pas par un
modele : elles ne disent rien de la qualite de l'extraction reelle. Tout ce
qu'il valide, c'est la plomberie.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODELS = ["mock-qwen3.6-35b", "mock-qwen3-vl-8b", "mock-bge-m3"]

# Lexique servant a produire une extraction plausible a partir du texte recu.
SKILL_LEXICON = [
    "Python", "Django", "Django REST Framework", "DRF", "Flask", "FastAPI",
    "PostgreSQL", "MySQL", "SQL", "Redis", "Docker", "Kubernetes", "Terraform",
    "JavaScript", "TypeScript", "React", "Vue", "Angular", "Next.js",
    "Java", "Spring Boot", "Go", "Rust", "C#", "PHP",
    "PyTorch", "TensorFlow", "LLM", "RAG", "Transformers", "Airflow", "Spark",
    "Git", "CI-CD", "Linux", "AWS", "Azure", "Celery", "GraphQL",
]
LANGUAGE_LEXICON = {
    "francais": "Francais", "français": "Francais",
    "anglais": "Anglais", "english": "Anglais",
    "arabe": "Arabe", "espagnol": "Espagnol", "allemand": "Allemand",
}

# Volume de raisonnement simule, calibre sur la mesure reelle : Qwen3.6-35B
# produit environ 390 tokens de reflexion avant de repondre a une question
# simple, contre 25 tokens sans raisonnement.
REASONING_TOKENS = 390
REASONING_TEXT = (
    "Voici un raisonnement simule.\n1. Analyse de la demande.\n"
    "2. Reperage des elements pertinents.\n3. Formulation de la reponse.\n"
) * 12

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# Motif volontairement simple : les quantificateurs imbriques d'une expression
# « generique » de numero de telephone provoquent un retour arriere exponentiel
# sur les chaines qui echouent.
PHONE_RE = re.compile(r"\+?\d[\d\s.-]{7,15}\d")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/\S+", re.I)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/\S+", re.I)
YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")


class MockConfig:
    """Reglages du banc d'essai, y compris ses modes degrades."""

    def __init__(
        self,
        *,
        models: list[str] | None = None,
        fail_rate: float = 0.0,
        reject_response_format: bool = False,
        reasoning: bool = False,
        latency_ms: int = 0,
        seed: int = 1234,
    ) -> None:
        self.models = models or DEFAULT_MODELS
        # Proportion d'appels repondant 503 : exerce la reprise du client.
        self.fail_rate = fail_rate
        # Simule un serveur ignorant `response_format` : exerce le repli
        # sur `guided_json`, comme vLLM < 0.6 ou certaines versions de SGLang.
        self.reject_response_format = reject_response_format
        # Simule un modele a raisonnement type Qwen3 : il ecrit d'abord dans
        # `reasoning_content`, et ne produit sa reponse qu'ensuite. Avec un
        # budget de tokens trop court, il rend un contenu vide et un
        # `finish_reason` a « length » — le comportement exact observe sur
        # Qwen3.6-35B, et celui qui avait fait echouer l'analyse redigee.
        self.reasoning = reasoning
        self.latency_ms = latency_ms
        self.random = random.Random(seed)


class _Handler(BaseHTTPRequestHandler):
    config: MockConfig

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        logger.debug("mock-inference %s", fmt % args)

    # -- protocole ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._respond(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": name, "object": "model", "owned_by": "banc-d-essai"}
                        for name in self.config.models
                    ],
                },
            )
            return
        self._respond(404, {"error": {"message": f"chemin inconnu : {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": {"message": "corps JSON invalide"}})
            return

        if self.config.latency_ms:
            threading.Event().wait(self.config.latency_ms / 1000)

        if self.config.random.random() < self.config.fail_rate:
            self._respond(503, {"error": {"message": "surcharge simulee"}})
            return

        path = self.path.rstrip("/")
        if path.endswith("/chat/completions"):
            self._chat(payload)
        elif path.endswith("/embeddings"):
            self._embeddings(payload)
        else:
            self._respond(404, {"error": {"message": f"chemin inconnu : {self.path}"}})

    # -- points d'entree ---------------------------------------------------
    def _chat(self, payload: dict) -> None:
        if "response_format" in payload and self.config.reject_response_format:
            self._respond(
                400,
                {
                    "error": {
                        "message": "response_format n'est pas pris en charge",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        schema = _requested_schema(payload)
        prompt = _prompt_text(payload)
        image_count = _image_count(payload)
        max_tokens = payload.get("max_tokens", 2048)

        thinking = self.config.reasoning and payload.get(
            "chat_template_kwargs", {}
        ).get("enable_thinking", True)

        reasoning = ""
        reasoning_tokens = 0
        if thinking:
            reasoning = REASONING_TEXT
            reasoning_tokens = REASONING_TOKENS
            if max_tokens <= reasoning_tokens:
                # Budget epuise en cours de reflexion : contenu vide et coupe.
                self._respond(
                    200,
                    {
                        "id": "mock-completion",
                        "object": "chat.completion",
                        "model": payload.get("model", self.config.models[0]),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "reasoning_content": reasoning,
                                },
                                "finish_reason": "length",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": len(prompt) // 4,
                            "completion_tokens": max_tokens,
                            "total_tokens": len(prompt) // 4 + max_tokens,
                        },
                    },
                )
                return

        if schema is None:
            content = (
                "Analyse produite par le banc d'essai. Le profil couvre les "
                "competences principales attendues ; un ecart subsiste sur les "
                "technologies d'infrastructure. La decision revient au recruteur."
            )
        else:
            content = json.dumps(
                _instance_from_schema(schema, prompt), ensure_ascii=False
            )

        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning

        prompt_tokens = len(prompt) // 4 + image_count * 800
        completion_tokens = len(content) // 4 + reasoning_tokens
        self._respond(
            200,
            {
                "id": "mock-completion",
                "object": "chat.completion",
                "model": payload.get("model", self.config.models[0]),
                "choices": [
                    {"index": 0, "message": message, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        )

    def _embeddings(self, payload: dict) -> None:
        inputs = payload.get("input") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        self._respond(
            200,
            {
                "object": "list",
                "model": payload.get("model", "mock-bge-m3"),
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": _pseudo_embedding(text),
                    }
                    for index, text in enumerate(inputs)
                ],
            },
        )

    def _respond(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


# --- Analyse de la requete --------------------------------------------------
def _requested_schema(payload: dict) -> dict | None:
    fmt = payload.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        return fmt.get("json_schema", {}).get("schema")
    return payload.get("guided_json")


def _prompt_text(payload: dict) -> str:
    parts: list[str] = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return "\n".join(parts)


def _image_count(payload: dict) -> int:
    count = 0
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            count += sum(
                1
                for block in content
                if isinstance(block, dict) and block.get("type") == "image_url"
            )
    return count


# --- Fabrication d'une instance conforme au schema --------------------------
def _instance_from_schema(schema: dict, context: str) -> Any:
    kind = schema.get("type")

    if kind == "object":
        properties = schema.get("properties", {})
        if {"identity", "skills"} <= set(properties):
            return _cv_instance(schema, context)
        return {
            name: _instance_from_schema(sub, context)
            for name, sub in properties.items()
        }

    if kind == "array":
        item = schema.get("items", {"type": "string"})
        return [_instance_from_schema(item, context) for _ in range(2)]

    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False

    if "enum" in schema:
        return schema["enum"][0]
    return _quote(context) if "evidence" in (schema.get("description") or "") else ""


def _cv_instance(schema: dict, context: str) -> dict:
    """Extraction plausible d'un CV, par regles simples.

    L'objectif n'est pas la qualite mais le realisme structurel : citations
    reellement presentes dans le document, dates coherentes, competences tirees
    d'un lexique. Cela suffit a exercer l'ancrage des preuves et la
    persistance de bout en bout.
    """
    sentences = _sentences(context)
    lowered = context.lower()

    email = EMAIL_RE.search(context)
    phone = PHONE_RE.search(context)
    linkedin = LINKEDIN_RE.search(context)
    github = GITHUB_RE.search(context)
    years = sorted({int(match) for match in YEAR_RE.findall(context)})

    skills = [name for name in SKILL_LEXICON if name.lower() in lowered][:12]
    languages = sorted(
        {label for token, label in LANGUAGE_LEXICON.items() if token in lowered}
    )

    return {
        "identity": {
            "full_name": _guess_name(context),
            "email": email.group(0) if email else "",
            "phone": phone.group(0).strip() if phone else "",
            "linkedin": linkedin.group(0) if linkedin else "",
            "github": github.group(0) if github else "",
            "location": _guess_location(context),
            "headline": sentences[1][:120] if len(sentences) > 1 else "",
        },
        "skills": [
            {
                "name": name,
                "years": 0,
                "last_used_year": years[-1] if years else 0,
                "evidence": _quote_containing(sentences, name),
            }
            for name in skills
        ],
        "experiences": [
            {
                "title": "Poste identifie par le banc d'essai",
                "company": "Entreprise",
                "location": "",
                "start_date": f"{years[0]}-01" if years else "",
                "end_date": f"{years[-1]}-01" if len(years) > 1 else "",
                "description": "",
                "evidence": _quote(context),
            }
        ]
        if years
        else [],
        "education": [
            {
                "degree": "Diplome identifie par le banc d'essai",
                "field_of_study": "",
                "institution": "",
                "level": 5,
                "graduation_year": years[-1] if years else 0,
                "evidence": _quote(context),
            }
        ]
        if years
        else [],
        "languages": [
            {
                "language": language,
                "level": "B2",
                "evidence": _quote_containing(sentences, language),
            }
            for language in languages
        ],
        "certifications": [],
    }


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[\n.;]+", text)]
    return [part for part in parts if len(part) >= 12]


def _quote(text: str) -> str:
    sentences = _sentences(text)
    return sentences[0][:110] if sentences else ""


def _quote_containing(sentences: list[str], needle: str) -> str:
    lowered = needle.lower()
    for sentence in sentences:
        if lowered in sentence.lower():
            return sentence[:110]
    return sentences[0][:110] if sentences else ""


def _guess_name(text: str) -> str:
    """Premiere suite de mots capitalises : convention de mise en page des CV."""
    for line in text.splitlines():
        candidate = line.strip()
        if not 4 <= len(candidate) <= 60:
            continue
        words = candidate.split()
        if (
            2 <= len(words) <= 4
            and all(word[:1].isupper() for word in words)
            and not any(char.isdigit() for char in candidate)
        ):
            return candidate.title()
    return "Candidat du banc d'essai"


def _guess_location(text: str) -> str:
    villes = ["Casablanca", "Rabat", "Tanger", "Marrakech", "Fes", "Paris", "Lyon"]
    lowered = text.lower()
    return next((ville for ville in villes if ville.lower() in lowered), "")


def _pseudo_embedding(text: str, dim: int = 64) -> list[float]:
    """Vecteur deterministe derive du texte, normalise.

    Ce n'est evidemment pas un embedding semantique : deux textes proches ne
    donnent pas des vecteurs proches. Il sert uniquement a verifier que le
    client sait appeler /v1/embeddings, lire la reponse et normaliser.
    """
    generator = random.Random(text)
    vector = [generator.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


# --- Cycle de vie -----------------------------------------------------------
class MockInferenceServer:
    """Serveur controlable, utilisable en contexte `with` dans les tests."""

    def __init__(self, config: MockConfig | None = None, port: int = 0) -> None:
        self.config = config or MockConfig()
        handler = type("BoundHandler", (_Handler,), {"config": self.config})
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> MockInferenceServer:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> MockInferenceServer:
        return self.start()

    def __exit__(self, *args) -> None:
        self.stop()
