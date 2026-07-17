"""
RAG retriever - query a platform's ChromaDB collection for relevant chunks.
"""
from __future__ import annotations

import os
import sys
import time
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_SCORE_FLOOR = 0.30

# ChromaDB builds/compacts HNSW indexes in the background after a large ingest.
# A query landing in that window can raise "Error loading hnsw index". These
# retries ride out that transient state instead of failing the whole turn.
_QUERY_RETRIES = 3
_QUERY_BACKOFF_S = 0.75


@lru_cache(maxsize=1)
def _get_embed_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2"), device="cpu")


@lru_cache(maxsize=1)
def _get_chroma_client():
    import chromadb
    chroma_dir = os.getenv("CHROMA_PERSIST_DIR", "./rag/chroma_db")
    return chromadb.PersistentClient(path=str(Path(chroma_dir)))


def _collection_space(collection) -> str:
    """
    The HNSW distance metric the collection was actually built with.

    Chroma defaults to 'l2' when create_collection() is called without an
    hnsw:space. Collections built before this was made explicit are therefore
    l2, and their distances must not be read as cosine distances.
    """
    try:
        cfg = collection.configuration_json or {}
        space = (cfg.get("hnsw") or {}).get("space")
        if space:
            return str(space).lower()
    except Exception:  # noqa: BLE001 - older/newer chroma may not expose this
        pass
    meta = getattr(collection, "metadata", None) or {}
    return str(meta.get("hnsw:space", "l2")).lower()


def _similarity(dist: float, space: str) -> float:
    """
    Convert a Chroma distance into cosine similarity in [0, 1].

    Embeddings are L2-normalized at both ingest and query time, so:
      - cosine: distance = 1 - cos      -> sim = 1 - distance
      - l2:     distance = 2 - 2 * cos  -> sim = 1 - distance / 2   (squared L2)
      - ip:     distance = -cos         -> sim = -distance

    Reading an l2 distance as if it were cosine (the pre-2026-07-15 bug) halves
    every score: a true 0.63 match reported as 0.27, which the score floor then
    silently discarded. That made retrieve() return [] for every query.
    """
    if space == "cosine":
        sim = 1.0 - dist
    elif space == "ip":
        sim = -dist
    else:  # l2 / squared euclidean on normalized vectors
        sim = 1.0 - dist / 2.0
    return max(0.0, min(1.0, sim))


def retrieve(platform: str, query: str, top_k: int | None = None) -> list[dict]:
    """
    Returns: [{"content": str, "source": str, "score": float}, ...]
    Filtered: score >= 0.30
    Sorted: descending by score
    top_k: defaults to int(os.getenv("RAG_TOP_K", 5))
    """
    if top_k is None:
        top_k = int(os.getenv("RAG_TOP_K", "5"))

    try:
        client = _get_chroma_client()
        collection = client.get_collection(f"cloud_agents_{platform}")
    except Exception:
        return []

    model = _get_embed_model()
    emb = model.encode([query], normalize_embeddings=True)[0].tolist()

    # Retry the vector query to survive transient HNSW/compactor errors. If it
    # still fails, degrade gracefully: return no RAG context so the agent can
    # still answer using the model + tools, rather than crashing the turn.
    res = None
    for attempt in range(_QUERY_RETRIES):
        try:
            res = collection.query(
                query_embeddings=[emb], n_results=max(top_k * 2, top_k)
            )
            break
        except Exception as exc:
            if attempt < _QUERY_RETRIES - 1:
                time.sleep(_QUERY_BACKOFF_S * (attempt + 1))
                continue
            print(
                f"[rag] retrieval unavailable for '{platform}' "
                f"({type(exc).__name__}: {str(exc)[:120]}). "
                "Answering without doc context.",
                file=sys.stderr,
            )
            return []

    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    space = _collection_space(collection)

    results: list[dict] = []
    for doc, meta, dist in zip(docs, metas, dists):
        score = _similarity(float(dist), space)
        if score < _SCORE_FLOOR:
            continue
        results.append({
            "content": doc,
            "source": (meta or {}).get("source", "unknown"),
            "score": score,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]
