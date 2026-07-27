"""Embeddings et recherche vectorielle.

Choix assume : pas de base vectorielle dediee au demarrage. Pour un volume
realiste (< 50 000 CV), un produit scalaire numpy sur des vecteurs normalises
prend quelques millisecondes et evite un service supplementaire. L'interface
`VectorIndex` isole ce choix — basculer sur pgvector plus tard ne touche que
ce fichier.

Deux fournisseurs :
  - "local"  : fastembed (ONNX quantifie, ~50 Mo, pas de torch, CPU) ;
  - "server" : endpoint /v1/embeddings du serveur d'inference.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable, Sequence

import httpx
import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)

BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    pass


class BaseEmbedder:
    dim: int

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        raise NotImplementedError

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


class LocalEmbedder(BaseEmbedder):
    """fastembed : ONNX quantifie, aucune dependance a torch."""

    def __init__(self, model_name: str, dim: int) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover
            raise EmbeddingError(
                "fastembed est absent. Installe-le avec :\n"
                '    pip install -e ".[local-embeddings]"\n'
                "ou bascule EMBEDDING_PROVIDER=server dans le .env."
            ) from exc
        self._model = TextEmbedding(model_name=model_name)
        self.dim = dim

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        vectors = np.asarray(list(self._model.embed(list(texts))), dtype=np.float32)
        return _normalize(vectors)


class ServerEmbedder(BaseEmbedder):
    """Endpoint /v1/embeddings du serveur d'inference."""

    def __init__(self, model_name: str, dim: int) -> None:
        self.base_url = settings.LLM["BASE_URL"].rstrip("/")
        self.api_key = settings.LLM.get("API_KEY") or "not-needed"
        self.model_name = model_name
        self.dim = dim
        # Meme raison que dans `client.py` : une connexion neuve par lot
        # coutait pres d'une seconde, contre quelques millisecondes reutilisee.
        self._http = httpx.Client(
            timeout=60, headers={"Authorization": f"Bearer {self.api_key}"}
        )

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = list(texts[start : start + BATCH_SIZE])
            response = self._http.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model_name, "input": batch},
            )
            if response.status_code >= 400:
                raise EmbeddingError(
                    f"/embeddings a repondu {response.status_code} : {response.text[:300]}"
                )
            payload = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors.extend(item["embedding"] for item in payload)
        return _normalize(np.asarray(vectors, dtype=np.float32))


@functools.lru_cache(maxsize=1)
def get_embedder() -> BaseEmbedder:
    config = settings.EMBEDDING
    provider = config["PROVIDER"].lower()
    if provider == "server":
        return ServerEmbedder(config["MODEL"], config["DIM"])
    return LocalEmbedder(config["MODEL"], config["DIM"])


# `lru_cache` ne memorise pas les exceptions : sans ce drapeau, chaque appel
# retenterait l'import de fastembed (~50 ms perdues par candidature scoree, et
# autant de lignes de log identiques).
_unavailable: str | None = None


def get_embedder_or_none() -> BaseEmbedder | None:
    """Renvoie le fournisseur d'embeddings, ou None s'il est indisponible.

    L'indisponibilite est memorisee et signalee une seule fois : elle degrade
    le service (rapprochement par ontologie seule) sans jamais l'interrompre.
    """
    global _unavailable
    if _unavailable is not None:
        return None
    try:
        return get_embedder()
    except Exception as exc:  # noqa: BLE001
        _unavailable = str(exc)
        logger.warning(
            "Embeddings indisponibles, rapprochement par ontologie seule : %s", exc
        )
        return None


def reset_availability() -> None:
    """Oublie un echec memorise. Utile apres une installation ou en test."""
    global _unavailable
    _unavailable = None
    get_embedder.cache_clear()


def _normalize(vectors: np.ndarray) -> np.ndarray:
    """Normalise en L2 : le produit scalaire devient une similarite cosinus."""
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32)


# --- Serialisation pour stockage en base -----------------------------------
def pack(vector: np.ndarray) -> bytes:
    """Vecteur -> bytes (BinaryField). Portable SQLite et PostgreSQL."""
    return np.asarray(vector, dtype=np.float32).tobytes()


def unpack(blob: bytes | memoryview | None) -> np.ndarray | None:
    if not blob:
        return None
    return np.frombuffer(bytes(blob), dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Similarite cosinus entre deux vecteurs deja normalises."""
    return float(np.dot(a, b))


def top_k(query: np.ndarray, matrix: np.ndarray, k: int = 10) -> list[tuple[int, float]]:
    """Renvoie les (indice, score) des k vecteurs les plus proches."""
    if matrix.size == 0:
        return []
    scores = matrix @ query
    k = min(k, len(scores))
    best = np.argpartition(-scores, k - 1)[:k]
    best = best[np.argsort(-scores[best])]
    return [(int(i), float(scores[i])) for i in best]


def stack(vectors: Iterable[np.ndarray | None]) -> tuple[np.ndarray, list[int]]:
    """Empile des vecteurs en ignorant les manquants.

    Renvoie la matrice et la liste des indices d'origine conserves, pour
    pouvoir remonter aux objets apres un `top_k`.
    """
    kept: list[np.ndarray] = []
    positions: list[int] = []
    for index, vector in enumerate(vectors):
        if vector is not None and vector.size:
            kept.append(vector)
            positions.append(index)
    if not kept:
        return np.empty((0, 0), dtype=np.float32), []
    return np.vstack(kept).astype(np.float32), positions
