"""HTTP interface to the index. This is what voice/ and fe/ consume.

Run: uv run python -m docsearch.serve
"""

from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from calling.router import calling_health, install_calling_router

from .embed import embed_query
from .store import DB_PATH, _connect, search

app = FastAPI(title="Prior-Auth Copilot policy search")
install_calling_router(app)

# The Next.js app calls this from the browser. Without these headers the browser
# blocks the response read, and any POST carrying Content-Type: application/json
# never reaches a handler at all: it forces a preflight OPTIONS, which returns
# 405 unless this middleware answers it. localhost and 127.0.0.1 are different
# origins to the browser, so both are listed. No allow_credentials: there is no
# auth here and claiming otherwise would be a security posture with nothing
# behind it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


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
    # The outbound demo does not require the legacy docsearch index.  Avoid
    # `_connect()` when it is absent because `_connect()` creates an empty
    # database, which would make health look superficially ready and turn a
    # simple local readiness probe into a filesystem mutation.
    chunks, payers = 0, 0
    docsearch = "unavailable"
    ollama = "not_checked"
    if DB_PATH.exists():
        try:
            conn = _connect()
            try:
                chunks, payers = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT payer) FROM chunks").fetchone()
            finally:
                conn.close()
            docsearch = "available"
            # Counting rows says nothing about whether a query can be served:
            # retain the warm probe for callers still using /search.
            try:
                embed_query("ping")
                ollama = "ok"
            except Exception as exc:
                print(f"[health error] ollama: {type(exc).__name__}: {exc}")
                ollama = "unreachable"
        except Exception as exc:
            print(f"[health error] docsearch: {type(exc).__name__}: {exc}")
            docsearch = "unavailable"
    return {
        "chunks": chunks,
        "payers": payers,
        "ollama": ollama,
        "docsearch": docsearch,
        "calling": calling_health(app),
    }


if __name__ == "__main__":
    import uvicorn

    # For LAN access use the uvicorn CLI, which takes the host flag:
    #   uv run uvicorn docsearch.serve:app --host 0.0.0.0
    uvicorn.run(app, host="127.0.0.1", port=8000)
