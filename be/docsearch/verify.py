"""Corpus-wide health check. Exits non-zero on any failure.

Run: uv run python -m docsearch.verify
"""

import re
import sys

from .embed import DIM, embed_query
from .index import CORPUS
from .store import _connect, _rank

_CHANGELOG = ("Added ", "Updated from ", "Removed ")
# Verbs that describe an edit to the policy. Not "Revised", which also appears
# as document metadata ("Status: Revised") on legitimate cover pages.
_CHANGE_RE = re.compile(r"\b(Added|Updated from|Removed)\b")
_NOT_NULL = ("id", "doc_key", "text", "payer", "payer_name", "drug",
             "plan_type", "page_start", "page_end", "chunk_index", "doc_url",
             "fmt", "scope", "embedding")
# The default top_k, so this checks what a consumer actually gets rather than a
# number picked because it passes.
_DEFAULT_TOP_K = 8


def main() -> int:
    conn = _connect()
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    (total,) = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
    check("chunks present", total > 0, f"{total} chunks")

    counts = dict(conn.execute(
        "SELECT doc_key, COUNT(*) FROM chunks GROUP BY doc_key").fetchall())
    expected = sorted(p.stem for p in CORPUS.glob("*.json"))
    print(f"{'document':<40} {'chunks':>7}")
    print("-" * 49)
    for key in expected:
        print(f"{key:<40} {counts.get(key, 0):>7}")
    missing = [k for k in expected if not counts.get(k)]
    check("every document contributed a chunk", not missing,
          "missing: " + ", ".join(missing) if missing else f"{len(expected)} documents")

    # A renamed or deleted corpus file leaves its rows queryable forever, and
    # the check above cannot see them because it only looks for absences.
    orphans = sorted(set(counts) - set(expected))
    check("no orphaned doc_key in the index", not orphans,
          ", ".join(orphans) if orphans else "none")

    (payers,) = conn.execute("SELECT COUNT(DISTINCT payer) FROM chunks").fetchone()
    check("distinct payers >= 2", payers >= 2, f"{payers} payers")

    # SQLite enforces NOT NULL on insert for every column below except id, where
    # a TEXT PRIMARY KEY is nullable. So this covers id, and guards against a
    # future SCHEMA edit dropping a constraint. It is not evidence about the
    # other thirteen columns.
    nulls = " OR ".join(f"{c} IS NULL" for c in _NOT_NULL)
    (n_null,) = conn.execute(f"SELECT COUNT(*) FROM chunks WHERE {nulls}").fetchone()
    check("no NULL in a NOT NULL column", n_null == 0, f"{n_null} rows")

    (n_bad,) = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE LENGTH(embedding) != ?",
        (DIM * 4,)).fetchone()
    check(f"every blob is {DIM * 4} bytes", n_bad == 0, f"{n_bad} rows wrong")

    # Self-retrieval proves the float32 round-trip, the unit-norm assumption,
    # the query/document prefix pairing and the ranking direction at once.
    # Deterministic sample and a rank-3 tolerance: about 1% of chunks are
    # near-duplicates of a chunk in a sibling document (the commercial and
    # medicaid twins share text verbatim) and lose rank 1 to their twin, which
    # made a random sample fail roughly one run in twenty for a benign reason.
    sample = conn.execute(
        "SELECT text FROM chunks ORDER BY id LIMIT 5").fetchall()
    misses = 0
    for (text,) in sample:
        hits = _rank(conn, embed_query(text), top_k=3)
        if text not in [h.text for h in hits]:
            misses += 1
    check("self-retrieval within rank 3 (5 chunks)", misses == 0,
          f"{len(sample) - misses}/{len(sample)}")

    # Ustekinumab has a dedicated document at all four payers, so this is the
    # cross-payer comparison the demo is built on.
    qvec = embed_query("ustekinumab initial approval criteria")
    found = sorted({h.payer for h in _rank(conn, qvec, top_k=_DEFAULT_TOP_K)})
    detail = f"top_k={_DEFAULT_TOP_K}: {', '.join(found)}"
    if len(found) < 4:
        deep = _rank(conn, qvec, top_k=100)
        ranks = {}
        for i, h in enumerate(deep, 1):
            ranks.setdefault(h.payer, i)
        detail += "  | first rank per payer: " + ", ".join(
            f"{p}@{r}" for p, r in sorted(ranks.items(), key=lambda x: x[1]))
    check("cross-payer retrieval hits all 4 payers at the default top_k",
          len(found) >= 4, detail)

    # Anchoring at the chunk start catches only about a third of changelog-
    # bearing chunks, measured by re-chunking with the trim disabled. Matching
    # anywhere, plus a dated revision row, raises recall enough that a trimming
    # regression actually trips this instead of sliding past it.
    pats = [f"%{p}%" for p in _CHANGELOG] + ["%Revised: %", "%Summary of Changes%"]
    like = " OR ".join(["text LIKE ?"] * len(pats))
    leaks = conn.execute(
        f"SELECT text FROM chunks WHERE {like}", pats).fetchall()
    dated = [t for (t,) in leaks
             if re.search(r"\d{1,2}/\d{1,2}/\d{4}", t) and _CHANGE_RE.search(t)]
    check("no revision-history leakage", not dated,
          f"{len(dated)} chunks with a dated changelog row"
          f" ({len(leaks)} matched a marker, headers included)")

    conn.close()

    print()
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<52} {detail}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
