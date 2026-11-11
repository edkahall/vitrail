"""Embeddings RAG (§XXVII) — 100% locaux via LiteLLM → Ollama (nomic-embed-text).

LiteLLM expose une API OpenAI-compatible (`/v1/embeddings`) qui route, selon le nom de
modèle, vers Ollama (`ollama/nomic-embed-text`, 768 dims) — entièrement local, sans clé API.
Recette éprouvée en production.

Deux usages :
  - **Indexation** (worker Celery) : `embed_texts` lève en cas d'échec (on veut savoir si
    l'indexation rate, pas indexer des chunks sans vecteur).
  - **Recherche RAG** (requête) : l'appelant attrape `EmbeddingError` et dégrade gracieusement
    (renvoie 0 résultat documentaire → l'agent répond avec ses autres outils / connaissances).

Le format de sortie OpenAI est `{"data": [{"embedding": [...], "index": i}, ...]}`. On trie par
`index` (LiteLLM/Ollama ne garantit pas l'ordre) pour réaligner sur l'entrée.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_BATCH_SIZE = 32          # textes par appel — borne le payload sans multiplier les requêtes
_DEFAULT_TIMEOUT = 60.0   # nomic est petit mais le 1er appel charge le modèle en mémoire


class EmbeddingError(RuntimeError):
    """Échec d'embedding (proxy/Ollama injoignable, modèle absent, réponse inattendue)."""


def _headers() -> dict[str, str]:
    if settings.CMS_LITELLM_API_KEY:
        return {"Authorization": f"Bearer {settings.CMS_LITELLM_API_KEY}"}
    return {}


def _embed_batch(client: httpx.Client, texts: list[str], model: str) -> list[list[float]]:
    try:
        resp = client.post(
            f"{settings.CMS_LITELLM_URL.rstrip('/')}/v1/embeddings",
            headers=_headers(),
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        rows = sorted(data["data"], key=lambda r: r.get("index", 0))
        vectors = [r["embedding"] for r in rows]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"incomplete response: {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors
    except EmbeddingError:
        raise
    except Exception as exc:  # noqa: BLE001 — on enveloppe toute erreur réseau/format
        raise EmbeddingError(f"embeddings failed via {settings.CMS_LITELLM_URL}: {exc}") from exc


def embed_texts(
    texts: list[str],
    *,
    model: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[list[float]]:
    """Embedde une liste de textes (batchée). Lève `EmbeddingError` en cas d'échec."""
    if not texts:
        return []
    mdl = model or settings.CMS_EMBED_MODEL
    out: list[list[float]] = []
    with httpx.Client(timeout=timeout) as client:
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            out.extend(_embed_batch(client, batch, mdl))
            logger.debug("embeddings: %d/%d", min(i + _BATCH_SIZE, len(texts)), len(texts))
    return out


def embed_text(
    text: str, *, model: str | None = None, timeout: float = _DEFAULT_TIMEOUT
) -> list[float]:
    """Embedde un seul texte (recherche RAG). Lève `EmbeddingError` en cas d'échec."""
    return embed_texts([text], model=model, timeout=timeout)[0]


def to_pgvector(vector: list[float]) -> str:
    """Sérialise un vecteur Python en littéral pgvector (`[0.1,0.2,...]`)."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
