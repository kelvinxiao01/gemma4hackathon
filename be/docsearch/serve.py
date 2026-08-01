"""HTTP interface to the index. This is what voice/ and fe/ consume.

Run: uv run python -m docsearch.serve
"""

from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query

from .embed import embed_query
from .store import _connect, search

app = FastAPI(title="Prior-Auth Copilot policy search")


# Plain def, not async def: Starlette runs it in a threadpool, which is what a
# sync function doing blocking I/O needs.
@app.get("/search")
def search_endpoint(q: str, payer: str | None = None, drug: str | None = None,
                    top_k: int = Query(8, ge=1, le=100)):
    # Ollama embeds an empty string happily and the index returns its 8 nearest
    # chunks, so a voice agent transcribing silence would get policy citations.
    if not q.strip():
        raise HTTPException(status_code=422, detail="q must not be empty")
    try:
        hits = search(q, payer=payer, drug=drug, top_k=top_k)
    except RuntimeError as exc:
        # Ollama rejected the input, most often a query past the 2048-token
        # ceiling. That is the caller's problem to fix, so say so.
        print(f"[search error] {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        print(f"[search error] {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=503, detail="policy index unavailable") from exc
    return {"hits": [asdict(h) for h in hits]}


@app.get("/health")
def health():
    conn = _connect()
    try:
        chunks, payers = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT payer) FROM chunks").fetchone()
    finally:
        conn.close()
    # Counting rows says nothing about whether a query can be served: with
    # Ollama down this returned 200 while every /search returned 500, and
    # consumers use this as their readiness probe. A warm probe is ~100 ms and
    # doubles as a keep-alive ping.
    try:
        embed_query("ping")
        ollama = "ok"
    except Exception as exc:
        print(f"[health error] ollama: {type(exc).__name__}: {exc}")
        ollama = "unreachable"
    return {"chunks": chunks, "payers": payers, "ollama": ollama}


if __name__ == "__main__":
    import uvicorn

    # For LAN access use the uvicorn CLI, which takes the host flag:
    #   uv run uvicorn docsearch.serve:app --host 0.0.0.0
    uvicorn.run(app, host="127.0.0.1", port=8000)
