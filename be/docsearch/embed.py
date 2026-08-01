"""EmbeddingGemma through the local Ollama server.

One seam for every embedding call, so a provider change is a rewrite of this
file rather than a refactor of the callers.
"""

import httpx

URL = "http://localhost:11434/api/embed"
MODEL = "embeddinggemma"
DIM = 768

# Verbatim from the model card. EmbeddingGemma is asymmetric: documents and
# queries take different prefixes and pairing them wrong costs retrieval quality.
_DOC_P = "title: none | text: "
_QUERY_P = "task: search result | query: "

_BATCH = 32

# Ollama embeds sequentially inside a batch, so batching buys HTTP overhead
# amortisation only. A cold load of the model from disk takes 8.3 s on this
# machine and httpx defaults to 5 s, which would surface as a mid-index
# ReadTimeout that reads like a broken server.
# ponytail: this is httpx's per-operation budget (connect, read, write, pool
# each get 120 s), not a wall-clock deadline. Read is per chunk received, so a
# trickling response can outlive it. Wrap the call in an outer deadline if a
# real ceiling is ever needed.
_TIMEOUT = 120.0


def _embed(texts: list[str], prefix: str) -> list[list[float]]:
    out: list[list[float]] = []
    with httpx.Client(timeout=_TIMEOUT) as client:
        for i in range(0, len(texts), _BATCH):
            batch = [prefix + t for t in texts[i:i + _BATCH]]
            r = client.post(URL, json={
                # truncate=False turns silent truncation at the 2048-token
                # ceiling into an HTTP 400 we can catch.
                "model": MODEL, "input": batch,
                "truncate": False, "keep_alive": -1,
            })
            # Not is_error, which passes 3xx through: follow_redirects is off by
            # default, so a redirect would reach r.json() and fail there as an
            # opaque KeyError. is_success is exactly raise_for_status's test,
            # and raising here keeps the body, which is where Ollama puts
            # "the input length exceeds the context length".
            if not r.is_success:
                # Deliberate override of the return-a-safe-value convention. An
                # empty return would silently truncate zip(texts, vectors) and
                # drop the document with no error surfacing anywhere.
                raise RuntimeError(f"[embed error] {r.status_code}: {r.text[:200]}")
            out.extend(r.json()["embeddings"])
    return out


def embed_docs(texts: list[str]) -> list[list[float]]:
    return _embed(texts, _DOC_P)


def embed_query(q: str) -> list[float]:
    return _embed([q], _QUERY_P)[0]
