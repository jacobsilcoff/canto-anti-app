"""Text embeddings — thin wrapper over Gemini's embedding model.

Pure service layer: embed text → vectors, cosine similarity, and pack/unpack
for BLOB storage. NO database access (db caching is orchestrated by the caller,
per the layering rule that all DB access goes through db.py). Reuses
translation's cached genai client so we don't open a second connection pool.

Used by the tutor's construction-drill builder: the LLM proposes the content
words a construction's examples need, we embed them and snap each to the nearest
word the learner already knows (so drills practice the form with familiar vocab).
"""
import asyncio
import math
from array import array

from google.genai import types

from translation import _get_client

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768                 # truncated from the model's default; plenty for snapping
_BATCH = 100                    # max contents per embed request


def _embed_sync(texts: list[str], api_key: str, model: str, dim: int) -> list[list[float]]:
    client = _get_client(api_key)
    cfg = types.EmbedContentConfig(output_dimensionality=dim)
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        chunk = texts[i:i + _BATCH]
        res = client.models.embed_content(model=model, contents=chunk, config=cfg)
        out.extend([list(e.values) for e in res.embeddings])
    return out


async def embed(texts: list[str], api_key: str, *,
                model: str = EMBED_MODEL, dim: int = EMBED_DIM) -> list[list[float]]:
    """Embed a list of texts → one vector each (order preserved). Empty in → empty out."""
    texts = [t for t in texts]
    if not texts:
        return []
    return await asyncio.to_thread(_embed_sync, texts, api_key, model, dim)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def nearest(vec: list[float], candidates: list[tuple[str, list[float]]]) -> tuple[str, float]:
    """Return (label, cosine) of the closest candidate, or ("", -1) if none."""
    best_label, best_score = "", -1.0
    for label, cvec in candidates:
        s = cosine(vec, cvec)
        if s > best_score:
            best_score, best_label = s, label
    return best_label, best_score


def pack(vec: list[float]) -> bytes:
    """Pack a float vector into compact float32 bytes for BLOB storage."""
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)
