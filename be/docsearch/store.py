"""Chunk storage and vector search over the local sqlite index."""

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .embed import DIM, embed_query

# Absolute. A consumer started from the repo root would otherwise create an
# empty database next to itself and answer "not found in policy" for every
# question while the real index sits three directories away.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "docsearch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, doc_key TEXT NOT NULL, text TEXT NOT NULL,
  payer TEXT NOT NULL, payer_name TEXT NOT NULL,
  drug TEXT NOT NULL, brand_name TEXT, plan_type TEXT NOT NULL,
  page_start INTEGER NOT NULL, page_end INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL, doc_url TEXT NOT NULL,
  fmt TEXT NOT NULL, scope TEXT NOT NULL,
  embedding BLOB NOT NULL)
"""

_COLS = ("text", "payer", "payer_name", "drug", "brand_name", "plan_type",
         "page_start", "page_end", "doc_url", "fmt", "scope")


@dataclass(frozen=True, slots=True)
class Hit:
    text: str
    score: float
    payer: str
    payer_name: str
    drug: str
    brand_name: str | None
    plan_type: str
    page_start: int
    page_end: int
    doc_url: str
    fmt: str = "pdf"                  # pdf | html. html has no real pages.
    scope: str = "dedicated"          # dedicated | omnibus. omnibus means the
                                      # drug label describes the document only.
    # The citation to render. None when the source has no pagination, so a
    # consumer cites doc_url instead. page_start is 1 on every html row because
    # the document is one page; printing "p.1" there is a fabricated citation,
    # and expecting every consumer to remember to branch on fmt is how that
    # ships. Read this field and the wrong thing becomes hard to do.
    page_label: str | None = None
    # ponytail: constants, not a sections layer. The lifted section detector was
    # cut because it compiled zero patterns against real text and its acceptance
    # check could not fail. Declared rather than dropped so downstream code that
    # reads them keeps working. Re-earning them means a detector with a check
    # that can fail; do not rebuild the one that was already rejected.
    section_name: str = "content"
    section_priority: str = "medium"
    is_table: bool = False


def _connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    # ponytail: a connection per call, opened and closed. At this corpus size
    # the open cost is irrelevant and it sidesteps thread affinity entirely
    # under Starlette's threadpool. Revisit only if /search gets real
    # concurrent load, at which point a pool needs its own transaction scope.
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(SCHEMA)
    return conn


def _like(text: str) -> str:
    """A LIKE pattern matching `text` literally.

    `%` and `_` are live wildcards inside a bound parameter. No drug name in
    this corpus contains either, and the exact-match IN clause gates the filter
    before this runs, so it is unreachable today. It is one line, and this is
    the clause that stops an omnibus document returning another drug's criteria.
    """
    escaped = text.translate(str.maketrans({"%": r"\%", "_": r"\_", "\\": "\\\\"}))
    return f"%{escaped}%"


def _synonyms(conn: sqlite3.Connection, drug: str) -> list[str]:
    """Every name the index knows for this drug, so `--drug Keytruda` finds the
    rows filed under `pembrolizumab` and vice versa."""
    d = drug.strip().lower()
    names = {d}
    for slug, brand in conn.execute(
            "SELECT DISTINCT drug, brand_name FROM chunks "
            "WHERE lower(drug) = ? OR lower(brand_name) = ?", (d, d)):
        names.update(n.lower() for n in (slug, brand) if n)
    return sorted(names)


def _rank(conn: sqlite3.Connection, qvec, payer=None, drug=None,
          top_k: int = 8) -> list[Hit]:
    where: list[str] = []
    params: list[str] = []

    # `is not None`, not truthiness: `?payer=` used to fall through as "no
    # filter at all", so a caller building the query string from an unset
    # variable got cross-payer results while believing it had filtered.
    if payer is not None:
        where.append("lower(payer) = ?")
        params.append(payer.strip().lower())

    if drug is not None:
        names = _synonyms(conn, drug)
        ph = ",".join("?" * len(names))
        where.append(f"(lower(drug) IN ({ph}) OR lower(brand_name) IN ({ph}))")
        params += names + names
        # On an omnibus document the drug label is document-level, so the label
        # alone hands back other drugs' criteria under the queried drug's name.
        # LIKE is already ASCII-case-insensitive, so no lower() is needed here.
        like = " OR ".join([r"text LIKE ? ESCAPE '\'"] * len(names))
        where.append(f"(scope = 'dedicated' OR {like})")
        params += [_like(n) for n in names]

    sql = f"SELECT {', '.join(_COLS)}, embedding FROM chunks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        # np.vstack([]) and np.array([]) @ q both raise; a filter that matches
        # nothing must not crash the caller mid-utterance.
        return []

    # ponytail: brute-force scan of every matching row. ~1 MB of float32 and
    # about a millisecond at this corpus size, against zero dependencies and no
    # index to keep in sync. Holds to roughly 100k chunks; past that, sqlite-vec.
    mat = np.frombuffer(b"".join(r[-1] for r in rows),
                        dtype=np.float32).reshape(len(rows), DIM)
    # Ollama returns L2-normalised vectors, so the bare dot product is cosine.
    scores = mat @ np.asarray(qvec, dtype=np.float32)
    # max(0, ...): a negative top_k is a Python slice bound, so [:-1] would
    # return every hit but the last instead of erroring.
    idx = np.argsort(-scores)[:max(0, top_k)]
    hits = []
    for i in idx:
        f = dict(zip(_COLS, rows[i]))
        hits.append(Hit(score=float(scores[i]),
                        page_label=f"p.{f['page_start']}" if f["fmt"] == "pdf" else None,
                        **f))
    return hits


def search(query: str, payer: str | None = None, drug: str | None = None,
           top_k: int = 8) -> list[Hit]:
    conn = _connect()
    try:
        return _rank(conn, embed_query(query), payer, drug, top_k)
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Query the local policy index.")
    ap.add_argument("query")
    ap.add_argument("--payer")
    ap.add_argument("--drug")
    ap.add_argument("--top-k", type=int, default=8)
    a = ap.parse_args()

    hits = search(a.query, payer=a.payer, drug=a.drug, top_k=a.top_k)
    if not hits:
        print("no hits")
        return
    for h in hits:
        cite = h.page_label or "no pagination"
        print(f"\n{h.score:.4f}  {h.payer_name} {h.plan_type}  "
              f"{h.drug} ({h.scope})  {cite}\n{h.doc_url}\n"
              f"{h.text[:300].strip()}")


if __name__ == "__main__":
    main()
