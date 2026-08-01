"""Build the sqlite index from the committed corpus. No network beyond Ollama.

Run: uv run python -m docsearch.index
"""

import json
import re
import sys
from pathlib import Path

import numpy as np

from .embed import DIM, embed_docs
from .store import _connect

CORPUS = Path(__file__).resolve().parent.parent / "corpus"

# 1500 chars is roughly 375 tokens, well under EmbeddingGemma's 2048 ceiling
# even once both prefixes are prepended.
# ponytail: characters as a token proxy. tiktoken would download its vocab from
# an OpenAI endpoint on first use, breaking the offline guarantee, and is the
# wrong tokenizer for a Gemma encoder anyway. The margin to 2048 is what makes
# the approximation safe; shrink the margin and this needs a real tokenizer.
MAX_CHUNK_CHARS = 1500

# Where the operative policy ends. Everything from the first qualifying match to
# the end of the document is dropped. Each marker carries the earliest fraction
# of the document it is allowed to match at, because all of these strings also
# appear in tables of contents near the front.
#
# Revision histories restate superseded criteria in the same wording as current
# ones, so retrieving one means citing withdrawn policy with a page number and
# URL behind it. Background sections are literature review (FDA approval
# history, trial narrative); on the Aetna HTML bulletins they are 68-82% of the
# document and were the single largest source of background-prose answers.
#
# ponytail: fitted to this corpus of 15 documents and hand-verified boundary by
# boundary. Re-verify every cut if the corpus is refetched or extended. The
# structural fix is detecting a changelog by its dated-row shape rather than by
# a heading string plus a position threshold.
_TAIL_MARKERS = tuple((re.compile(p), floor) for p, floor in (
    (r"(?m)^Background[ \t]*$", 0.10),
    (r"Revision Details", 0.55),
    (r"Policy History/Revision Information", 0.55),
    # Anthem writes the heading twice, once bare and once with a colon above the
    # entries. Matching only the colon form left the bare heading and its
    # "Revised: <date>" line stranded on the tail of the last kept chunk.
    (r"(?m)^Document History[ \t]*:?[ \t]*$", 0.55),
    (r"Document History:", 0.55),
    (r"Revision History", 0.55),
    (r"Summary of Changes", 0.55),
))


def chunk_page(text: str) -> list[str]:
    """Split within one page, on single newlines.

    Both pdfplumber and trafilatura emit one \\n per line and never a bare
    blank line, so splitting on blank lines would degenerate into fixed-width
    slicing and start most chunks mid-sentence.

    ponytail: chunks are disjoint, no overlap. A criterion straddling a boundary
    is retrievable from neither half at full strength. Correct at 15 documents;
    add overlap in the accumulator if recall ever matters more than index size.
    """
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        # A line longer than the budget cannot be packed, so break it at the
        # last space that fits rather than at a fixed offset. Slicing blind put
        # 20% of chunks mid-word, which reads as broken when spoken aloud.
        while len(line) > MAX_CHUNK_CHARS:
            if buf:
                chunks.append(buf)
                buf = ""
            cut = line.rfind(" ", 0, MAX_CHUNK_CHARS + 1)
            if cut <= 0:
                cut = MAX_CHUNK_CHARS       # one unbroken token, no choice
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        if buf and len(buf) + 1 + len(line) > MAX_CHUNK_CHARS:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return [c for c in (c.strip() for c in chunks) if c]


def trim_tail(pages: list[dict]) -> tuple[list[dict], int]:
    """Cut at the first qualifying tail marker and drop every later page.

    Returns the kept pages and the number of characters dropped. Cutting at the
    marker's offset rather than at the page boundary keeps the legitimate
    content sitting above it on the same page.
    """
    total = sum(len(p["text"]) for p in pages)
    seen = 0
    for i, p in enumerate(pages):
        # finditer, not find: a marker that also appears earlier on this page
        # (a table of contents entry) must not mask the real section below it.
        offs = [m.start() for rx, floor in _TAIL_MARKERS
                for m in rx.finditer(p["text"])
                if (seen + m.start()) / total > floor]
        if offs:
            cut = min(offs)
            kept = pages[:i] + [{"page": p["page"], "text": p["text"][:cut]}]
            return kept, total - (seen + cut)
        seen += len(p["text"])
    return pages, 0


def _rejection_reason(rows: list[tuple[int, str]], vecs: list[list[float]],
                      n_pages: int) -> str | None:
    """Reject the document rather than index a broken one. Returns a reason."""
    if not rows:
        return "no chunks"
    # Bound at the chunker's own budget, not at a looser number. This fires the
    # moment anyone loosens chunk_page, which is the regression it is here for.
    if max(len(t) for _, t in rows) > MAX_CHUNK_CHARS:
        return f"chunk over {MAX_CHUNK_CHARS} chars"
    # page_start == page_end by construction, so only the range needs checking.
    bad = [pg for pg, _ in rows if not 1 <= pg <= n_pages]
    if bad:
        return f"page {bad[0]} outside 1..{n_pages}"
    if len(vecs) != len(rows):
        return f"{len(vecs)} vectors for {len(rows)} chunks"
    arr = np.asarray(vecs, dtype=np.float32)
    if arr.shape != (len(rows), DIM):
        return f"vector shape {arr.shape}, expected {(len(rows), DIM)}"
    if not np.isfinite(arr).all():
        return "non-finite value in a vector"
    norms = np.linalg.norm(arr, axis=1)
    if not ((norms > 0.99) & (norms < 1.01)).all():
        # Without unit norms the dot product in search() is not cosine and
        # every score is quietly wrong.
        return f"norms {norms.min():.4f}..{norms.max():.4f}, expected ~1.0"
    return None


def index_doc(conn, doc: dict) -> tuple[str, int, int]:
    key = f"{doc['payer']}_{doc['drug']}_{doc['plan_type']}"
    pages, dropped = trim_tail(doc["pages"])

    rows = [(p["page"], c) for p in pages for c in chunk_page(p["text"])]

    # An omnibus document's drug label is document-level. Putting it in the
    # prefix would inject a false drug signal into ~80% of its vectors.
    label = f"{doc['payer_name']} {doc['plan_type'].title()}"
    if doc["scope"] == "dedicated":
        label += f" | Drug: {doc['drug']}"
    prefix = f"[{label}] "

    vecs = embed_docs([prefix + t for _, t in rows]) if rows else []

    reason = _rejection_reason(rows, vecs, doc["n_pages"])
    if reason:
        raise ValueError(reason)

    blobs = [np.asarray(v, dtype=np.float32).tobytes() for v in vecs]

    # Content-hashed ids are not idempotent: any change to chunking or the
    # prefix mints new ids, and INSERT OR REPLACE then appends a second
    # generation of every chunk instead of replacing the first.
    conn.execute("DELETE FROM chunks WHERE doc_key = ?", (key,))
    conn.executemany(
        "INSERT INTO chunks (id, doc_key, text, payer, payer_name, drug,"
        " brand_name, plan_type, page_start, page_end, chunk_index, doc_url,"
        " fmt, scope, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(f"{key}_{i:04d}", key, text, doc["payer"], doc["payer_name"],
          doc["drug"], doc["brand_name"], doc["plan_type"], page, page, i,
          doc["doc_url"], doc["fmt"], doc["scope"], blob)
         for i, ((page, text), blob) in enumerate(zip(rows, blobs))])

    # Reads the uncommitted rows on this same connection, which is the point:
    # it proves the bytes survived the float32 round trip through sqlite.
    (blob,) = conn.execute(
        "SELECT embedding FROM chunks WHERE id = ?", (f"{key}_0000",)).fetchone()
    if len(blob) != DIM * 4:
        raise ValueError(f"blob is {len(blob)} bytes, expected {DIM * 4}")
    if not np.allclose(np.frombuffer(blob, dtype=np.float32), vecs[0]):
        raise ValueError("float32 blob round-trip mismatch")

    conn.commit()
    return key, len(rows), dropped


def main() -> int:
    conn = _connect()
    print(f"{'document':<40} {'chunks':>7} {'dropped':>8}  status")
    print("-" * 74)
    total = failed = 0
    for path in sorted(CORPUS.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            key, n, dropped = index_doc(conn, doc)
        except Exception as exc:
            # Without the rollback, this document's uncommitted DELETE and
            # INSERT ride along on the next document's commit, so a document
            # reported as failed ends up live in the index. The follow-up
            # DELETE then guarantees a failed document leaves nothing behind,
            # including a previous run's rows.
            conn.rollback()
            conn.execute("DELETE FROM chunks WHERE doc_key = ?", (path.stem,))
            conn.commit()
            failed += 1
            print(f"[index error] {path.stem}: {type(exc).__name__}: {exc}")
            continue
        total += n
        print(f"{key:<40} {n:>7} {dropped:>8}  ok")

    # A renamed or deleted corpus file leaves its rows behind forever: the
    # per-document DELETE only touches keys the current corpus still names.
    # This project already relabelled two documents once, and the stale rows
    # stayed queryable through every existing check.
    stems = [p.stem for p in CORPUS.glob("*.json")]
    ph = ",".join("?" * len(stems))
    orphans = [k for (k,) in conn.execute(
        f"SELECT DISTINCT doc_key FROM chunks WHERE doc_key NOT IN ({ph})", stems)]
    if orphans:
        conn.executemany("DELETE FROM chunks WHERE doc_key = ?",
                         [(k,) for k in orphans])
        conn.commit()
        print(f"\nremoved {len(orphans)} orphaned document(s): {', '.join(orphans)}")

    conn.close()
    print(f"\n{total} chunks indexed, {failed} document(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
