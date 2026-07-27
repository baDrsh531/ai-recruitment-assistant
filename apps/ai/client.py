"""Client d'inference compatible OpenAI (vLLM / SGLang / Ollama).

Deux endpoints distincts sont exposes : `chat_client()` pour le modele texte
(Qwen3.6 35B) et `vision_client()` pour le modele vision-langage (Qwen3-VL 8B),
utilise sur les CV scannes ou a mise en page complexe.

Regle du projet : aucun appel ne renvoie du texte libre a parser a la main.
Toute extraction passe par `schema=` (decodage contraint par JSON Schema).
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 3.0)
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Connexions gardees ouvertes entre deux appels. Ouvrir une connexion neuve a
# chaque requete coutait environ une seconde par appel modele — mesure contre
# le serveur d'essai — alors qu'une connexion reutilisee repond en 3 ms.
# L'extraction d'un seul CV enchaine plusieurs appels : le surcout etait paye
# a chaque fois, et une nouvelle fois a chaque tentative de reprise.
POOL_LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)

CHAT_PATH = "/chat/completions"


class InferenceError(RuntimeError):
    """Le serveur d'inference n'a pas pu repondre."""


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    latency_ms: int
    parsed: Any = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 1
    # Raisonnement interne des modeles de type Qwen3 : ce n'est pas la reponse,
    # mais il est facture et il explique l'essentiel du cout quand il est actif.
    reasoning: str = ""
    finish_reason: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def strict_schema(schema: dict) -> dict:
    """Durcit un JSON Schema pour le decodage contraint.

    Les moteurs de guided decoding tolerent mal les champs optionnels et les
    proprietes libres : on force `additionalProperties: false` et on rend tous
    les champs requis, recursivement.
    """
    schema = json.loads(json.dumps(schema))  # copie profonde

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


def image_part(image_bytes: bytes, mime: str = "image/png") -> dict:
    """Construit un bloc image pour le modele vision."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


class InferenceClient:
    """Enveloppe httpx autour d'une API /v1 compatible OpenAI."""

    def __init__(self, config: dict, *, kind: str) -> None:
        if not config.get("BASE_URL"):
            raise InferenceError(
                f"Endpoint {kind} non configure. Renseigne "
                f"{'LLM' if kind != 'vision' else 'VLM'}_BASE_URL dans le .env, "
                "puis verifie avec `python manage.py check_ai`."
            )
        self.base_url = config["BASE_URL"].rstrip("/")
        self.model = config["MODEL"]
        self.api_key = config.get("API_KEY") or "not-needed"
        self.timeout = config.get("TIMEOUT", 120)
        self.kind = kind
        self._http: httpx.Client | None = None
        self._lock = threading.Lock()

    # -- bas niveau --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @property
    def http(self) -> httpx.Client:
        """Client HTTP partage, cree a la premiere utilisation.

        `httpx.Client` est sur pour un usage concurrent ; le verrou ne protege
        que sa creation, pour eviter d'en ouvrir deux depuis deux threads.
        """
        if self._http is None:
            with self._lock:
                if self._http is None:
                    self._http = httpx.Client(
                        timeout=self.timeout,
                        limits=POOL_LIMITS,
                        headers=self._headers(),
                    )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.http.post(url, json=payload)
                if response.status_code in RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json() | {"_attempts": attempt}
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code not in RETRYABLE_STATUS:
                    detail = exc.response.text[:500]
                    raise InferenceError(
                        f"{url} a repondu {exc.response.status_code} : {detail}"
                    ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc

            if attempt < MAX_ATTEMPTS:
                delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                logger.warning("Inference %s : tentative %s echouee (%s), nouvel essai dans %ss",
                               url, attempt, last_error, delay)
                time.sleep(delay)

        raise InferenceError(f"{url} injoignable apres {MAX_ATTEMPTS} tentatives : {last_error}")

    # -- haut niveau -------------------------------------------------------
    def list_models(self) -> list[str]:
        response = self.http.get(f"{self.base_url}/models", timeout=10)
        response.raise_for_status()
        return [item["id"] for item in response.json().get("data", [])]

    def chat(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        schema_name: str = "reponse",
        temperature: float = 0.0,
        max_tokens: int = 2048,
        thinking: bool = False,
        purpose: str = "generic",
        prompt_id: str = "",
        prompt_version: str = "",
        subject: Any = None,
        record: bool = True,
    ) -> LLMResponse:
        """Appelle le modele.

        `thinking` gouverne le raisonnement interne des modeles qui en ont un
        (Qwen3 et suivants). Il est desactive par defaut : sur une extraction
        structuree, il ne change pas le resultat et le multiplie par seize le
        nombre de tokens generes — 396 contre 25, mesure sur Qwen3.6-35B. Les
        taches ou une chaine de raisonnement apporte vraiment quelque chose
        doivent l'activer explicitement.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        hardened = strict_schema(schema) if schema else None
        if hardened:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": hardened, "strict": True},
            }

        input_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

        started = time.perf_counter()

        # Toute la sequence est sous la meme surveillance : l'appel reseau,
        # mais aussi la lecture de la reponse. Une reponse tronquee ou un JSON
        # malforme sont des echecs d'appel modele, et le journal d'audit doit
        # les contenir au meme titre qu'un timeout — sans quoi une analyse
        # manquante ne laisse aucune trace expliquant pourquoi.
        try:
            data = self._send(payload, hardened)
            response = self._read(data, max_tokens=max_tokens, hardened=hardened)
        except InferenceError as exc:
            if record:
                self._record(
                    purpose=purpose, prompt_id=prompt_id, prompt_version=prompt_version,
                    input_hash=input_hash, temperature=temperature, thinking=thinking,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    attempts=MAX_ATTEMPTS, subject=subject, error=str(exc),
                )
            raise

        response.latency_ms = int((time.perf_counter() - started) * 1000)
        if record:
            self._record(
                purpose=purpose, prompt_id=prompt_id, prompt_version=prompt_version,
                input_hash=input_hash, temperature=temperature,
                latency_ms=response.latency_ms,
                attempts=response.attempts, subject=subject, thinking=thinking,
                finish_reason=response.finish_reason,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
        return response

    def _send(self, payload: dict, hardened: dict | None) -> dict:
        """Envoie la requete, en retirant les extensions que le serveur refuse."""
        try:
            return self._post(CHAT_PATH, payload)
        except InferenceError as exc:
            message = str(exc)
            # Tous les serveurs n'acceptent pas les memes extensions. On retire
            # celle qui est refusee et on retente, plutot que d'echouer sur une
            # option accessoire.
            if "chat_template_kwargs" in message and "chat_template_kwargs" in payload:
                logger.info("chat_template_kwargs refuse, nouvel essai sans")
                payload.pop("chat_template_kwargs")
                return self._post(CHAT_PATH, payload)
            if hardened is not None and "response_format" in message:
                # vLLM < 0.6 et certaines versions de SGLang ignorent
                # `response_format` mais acceptent `guided_json`.
                logger.info("response_format refuse, repli sur guided_json")
                payload.pop("response_format")
                payload["guided_json"] = hardened
                return self._post(CHAT_PATH, payload)
            raise

    def _read(self, data: dict, *, max_tokens: int, hardened: dict | None) -> LLMResponse:
        choice = data["choices"][0]
        message = choice.get("message", {})
        text = (message.get("content") or "").strip()
        reasoning = (
            message.get("reasoning_content") or message.get("reasoning") or ""
        ).strip()
        finish_reason = choice.get("finish_reason", "")
        usage = data.get("usage") or {}

        # Une reponse tronquee doit se signaler comme telle. Sans cela, un
        # modele a raisonnement qui epuise son budget en reflechissant renvoie
        # un contenu vide, et l'erreur remontee parle de JSON malforme — ce qui
        # envoie chercher le probleme au mauvais endroit.
        if finish_reason == "length":
            detail = (
                f" Le modele a produit {len(reasoning)} caracteres de raisonnement "
                "avant d'etre coupe : augmente max_tokens ou laisse thinking a False."
                if reasoning
                else " Augmente max_tokens."
            )
            raise InferenceError(
                f"Reponse tronquee apres {usage.get('completion_tokens', '?')} "
                f"tokens (max_tokens={max_tokens})." + detail
            )

        parsed = None
        if hardened:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InferenceError(
                    f"Le modele devait renvoyer du JSON conforme, recu : {text[:300]!r}"
                ) from exc

        return LLMResponse(
            text=text,
            model=data.get("model", self.model),
            latency_ms=0,
            parsed=parsed,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            attempts=data.get("_attempts", 1),
            reasoning=reasoning,
            finish_reason=finish_reason,
            raw=data,
        )

    def _record(self, *, subject=None, error: str = "", **fields) -> None:
        from .models import AIInvocation

        try:
            AIInvocation.objects.create(
                kind=self.kind,
                model=self.model,
                base_url=self.base_url,
                status=AIInvocation.Status.ERROR if error else AIInvocation.Status.OK,
                error=error[:2000],
                subject_type=subject.__class__.__name__ if subject is not None else "",
                subject_id=str(getattr(subject, "pk", "")) if subject is not None else "",
                **fields,
            )
        except Exception:  # noqa: BLE001 — la tracabilite ne doit jamais casser l'appel
            logger.exception("Echec d'enregistrement de l'appel IA")


@functools.lru_cache(maxsize=8)
def _shared_client(
    base_url: str, model: str, api_key: str, timeout: int, kind: str
) -> InferenceClient:
    """Instance partagee, pour que le pool de connexions survive aux appels.

    La cle est faite des reglages eux-memes : modifier le .env ou surcharger
    les settings en test produit naturellement une autre instance, sans avoir
    a vider le cache a la main.
    """
    return InferenceClient(
        {"BASE_URL": base_url, "MODEL": model, "API_KEY": api_key, "TIMEOUT": timeout},
        kind=kind,
    )


def _from_settings(config: dict, kind: str) -> InferenceClient:
    if not config.get("BASE_URL"):
        # Message d'erreur explicite plutot qu'une cle de cache vide.
        return InferenceClient(config, kind=kind)
    return _shared_client(
        config["BASE_URL"],
        config.get("MODEL", ""),
        config.get("API_KEY") or "not-needed",
        int(config.get("TIMEOUT", 120)),
        kind,
    )


def chat_client() -> InferenceClient:
    return _from_settings(settings.LLM, kind="chat")


def vision_client() -> InferenceClient:
    return _from_settings(settings.VLM, kind="vision")
