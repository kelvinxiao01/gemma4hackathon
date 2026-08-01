"""Runs in any checkout: no database on disk, no Ollama."""

import numpy as np
import pytest

from docsearch.embed import DIM
from docsearch.index import (MAX_CHUNK_CHARS, _rejection_reason, chunk_page,
                            trim_tail)
from docsearch.store import _connect, _rank, _synonyms


def _unit(*idx):
    v = np.zeros(DIM, dtype=np.float32)
    for i in idx:
        v[i] = 1.0
    return v / np.linalg.norm(v)


def _insert(conn, rows):
    """rows: (id, payer, drug, brand, scope, text, vector)"""
    conn.executemany(
        "INSERT INTO chunks (id, doc_key, text, payer, payer_name, drug,"
        " brand_name, plan_type, page_start, page_end, chunk_index, doc_url,"
        " fmt, scope, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(rid, "doc", text, payer, payer.title(), drug, brand, "commercial",
          1, 1, 0, "https://example.test/x.pdf", "pdf", scope, v.tobytes())
         for rid, payer, drug, brand, scope, text, v in rows])
    conn.commit()


# --- chunker ----------------------------------------------------------------

def test_chunker_splits_a_long_page_on_line_boundaries():
    page = "\n".join(["Criterion line of policy text." * 3] * 60)  # ~5700 chars
    chunks = chunk_page(page)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    # Line accumulation, not fixed-width slicing: no chunk starts mid-word.
    assert all(c.startswith("Criterion") for c in chunks)


def test_chunker_keeps_every_character_of_the_page():
    """Reconstruction, not just bounds.

    Dropping the trailing buffer loses the tail of most pages, which is 17% of
    the real corpus, and every bounds-only assertion still passes.
    """
    page = "\n".join(f"line {i} of the coverage policy document" for i in range(400))
    joined = "".join(chunk_page(page)).replace("\n", "")
    assert joined == page.replace("\n", "")


def test_chunker_hard_splits_a_single_overlong_line():
    chunks = chunk_page("x" * (MAX_CHUNK_CHARS * 2 + 5))
    assert len(chunks) == 3
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)


def test_chunker_breaks_a_long_line_at_a_space_not_mid_word():
    page = "criterion " * 400          # 4000 chars, one line, spaces throughout
    chunks = chunk_page(page)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    # Every chunk starts and ends on a word boundary.
    assert all(c.startswith("criterion") and c.endswith("criterion")
               for c in chunks)


def test_chunker_drops_empty_pages():
    assert chunk_page("") == []
    assert chunk_page("\n  \n\n") == []


# --- tail trimming ----------------------------------------------------------

def test_trim_cuts_at_the_marker_and_drops_later_pages():
    pages = [{"page": 1, "text": "Coverage criteria. " * 40},
             {"page": 2, "text": "More criteria.\nRevision History\nAdded X."},
             {"page": 3, "text": "Updated from Y."}]
    kept, dropped = trim_tail(pages)
    assert [p["page"] for p in kept] == [1, 2]
    assert kept[-1]["text"] == "More criteria.\n"
    assert dropped == len("Revision History\nAdded X.") + len("Updated from Y.")


def test_trim_ignores_a_marker_near_the_front():
    pages = [{"page": 1, "text": "Revision History"},
             {"page": 2, "text": "Coverage criteria. " * 60}]
    kept, dropped = trim_tail(pages)
    assert kept == pages and dropped == 0


def test_trim_finds_the_real_marker_below_a_table_of_contents_entry():
    """find() would stop at the front-matter hit and leave the changelog in."""
    pages = [{"page": 1, "text": "Contents\nRevision History .... 12\n"
                                 + "body text here. " * 200
                                 + "Revision History\nAdded: superseded X.\n"}]
    kept, dropped = trim_tail(pages)
    assert dropped > 0
    assert "Added: superseded X." not in kept[0]["text"]
    assert "body text here." in kept[0]["text"]


def test_trim_cuts_a_background_section():
    pages = [{"page": 1, "text": "Criteria. " * 50 + "\nBackground\n"
                                 + "FDA approved indications narrative. " * 40}]
    kept, dropped = trim_tail(pages)
    assert dropped > 0
    assert "narrative" not in kept[0]["text"]
    assert "Criteria." in kept[0]["text"]


@pytest.mark.parametrize("pages", [
    [],
    [{"page": 1, "text": ""}],
    [{"page": 1, "text": ""}, {"page": 2, "text": ""}],
])
def test_trim_survives_a_document_with_no_text(pages):
    """The ratio threshold divides by the document total. It is only reached
    behind a marker match, which cannot happen with no text, but that is a
    short-circuit argument and short-circuit arguments deserve a test."""
    assert trim_tail(pages) == (pages, 0)


def test_trim_leaves_background_used_as_prose_alone():
    """Only a bare `Background` heading is a marker, not the word in a sentence."""
    pages = [{"page": 1, "text": "Criteria. " * 50
                                 + "Background therapy with steroids was allowed. " * 20}]
    kept, dropped = trim_tail(pages)
    assert dropped == 0 and kept == pages


# --- document validation ----------------------------------------------------

def test_rejection_reason_accepts_a_healthy_document():
    rows = [(1, "text one"), (1, "text two")]
    vecs = [_unit(0).tolist(), _unit(1).tolist()]
    assert _rejection_reason(rows, vecs, 1) is None


@pytest.mark.parametrize("rows, vecs, n_pages, fragment", [
    ([], [], 1, "no chunks"),
    ([(1, "a")], [], 1, "0 vectors"),
    ([(1, "a")], [[0.0] * (DIM - 1) + [1.0], [0.0] * DIM], 1, "vectors"),
    ([(9, "a")], [_unit(0).tolist()], 1, "outside"),
    ([(1, "a")], [[0.5] * DIM], 1, "norms"),
    ([(1, "a")], [[float("nan")] * DIM], 1, "non-finite"),
    ([(1, "x" * (MAX_CHUNK_CHARS + 1))], [_unit(0).tolist()], 1, "chunk over"),
])
def test_rejection_reason_rejects(rows, vecs, n_pages, fragment):
    reason = _rejection_reason(rows, vecs, n_pages)
    assert reason is not None and fragment in reason


# --- store ------------------------------------------------------------------

def test_payer_filter_and_ordering():
    conn = _connect(":memory:")
    _insert(conn, [
        ("a", "aetna", "pembrolizumab", "Keytruda", "dedicated", "exact", _unit(0)),
        ("b", "aetna", "pembrolizumab", "Keytruda", "dedicated", "partial", _unit(0, 1)),
        ("c", "cigna", "pembrolizumab", "Keytruda", "dedicated", "foreign", _unit(0)),
    ])
    q = _unit(0)

    hits = _rank(conn, q, payer="Aetna")
    assert [h.text for h in hits] == ["exact", "partial"]
    assert hits[0].score > hits[1].score

    assert _rank(conn, q, payer="nonexistent") == []
    assert len(_rank(conn, q, top_k=99)) == 3
    conn.close()


def test_top_k_truncates_and_rejects_a_negative_bound():
    conn = _connect(":memory:")
    _insert(conn, [(str(i), "aetna", "d", "B", "dedicated", f"t{i}", _unit(i))
                   for i in range(6)])
    q = _unit(0)
    assert len(_rank(conn, q, top_k=2)) == 2
    assert len(_rank(conn, q, top_k=0)) == 0
    # A raw slice would read this as "all but the last" and return 5.
    assert len(_rank(conn, q, top_k=-1)) == 0
    conn.close()


def test_empty_string_filter_is_a_filter_not_a_wildcard():
    conn = _connect(":memory:")
    _insert(conn, [("a", "aetna", "d", "B", "dedicated", "t", _unit(0))])
    assert _rank(conn, _unit(0), payer="") == []
    assert _rank(conn, _unit(0), payer=None) != []
    conn.close()


def test_synonyms_resolve_brand_to_slug_and_back():
    conn = _connect(":memory:")
    _insert(conn, [("a", "aetna", "pembrolizumab", "Keytruda", "dedicated",
                    "t", _unit(0))])
    assert _synonyms(conn, "Keytruda") == ["keytruda", "pembrolizumab"]
    assert _synonyms(conn, "pembrolizumab") == ["keytruda", "pembrolizumab"]
    # Nothing matched: the token itself, so the IN clause simply finds no row.
    assert _synonyms(conn, "aspirin") == ["aspirin"]
    conn.close()


def test_drug_filter_is_scope_aware():
    conn = _connect(":memory:")
    _insert(conn, [
        ("a", "aetna", "pembrolizumab", "Keytruda", "dedicated",
         "no drug named here", _unit(0)),
        ("b", "cigna", "pembrolizumab", "Keytruda", "omnibus",
         "Keytruda requires PD-L1 testing", _unit(0)),
        ("c", "cigna", "pembrolizumab", "Keytruda", "omnibus",
         "criteria for some other drug", _unit(0)),
    ])
    hits = _rank(conn, _unit(0), drug="Keytruda")
    # On the dedicated document the label is true of every chunk, so the row
    # stays even without the word. The omnibus row that never names the drug
    # holds another drug's criteria and is excluded.
    assert sorted(h.text for h in hits) == [
        "Keytruda requires PD-L1 testing", "no drug named here"]
    conn.close()


def test_like_wildcards_in_a_drug_argument_are_literal():
    conn = _connect(":memory:")
    _insert(conn, [("a", "cigna", "drug_a", "Brand", "omnibus",
                    "this text mentions drugXa only", _unit(0))])
    # An unescaped `_` would match the X and let a chunk that never names the
    # drug through the omnibus guard.
    assert _rank(conn, _unit(0), drug="drug_a") == []
    conn.close()


def test_float32_blob_round_trip_through_sqlite():
    """The numpy round-trip alone cannot fail. The BLOB column is the risk:
    a float32 read of a float64 blob yields nonsense floats with no exception."""
    conn = _connect(":memory:")
    v = _unit(3, 7, 11)
    _insert(conn, [("a", "aetna", "d", "B", "dedicated", "t", v)])
    (blob,) = conn.execute("SELECT embedding FROM chunks").fetchone()
    assert len(blob) == DIM * 4
    assert np.allclose(np.frombuffer(blob, dtype=np.float32), v)
    conn.close()


def test_hit_carries_fmt_and_scope():
    conn = _connect(":memory:")
    _insert(conn, [("a", "aetna", "d", "B", "omnibus", "t", _unit(0))])
    (hit,) = _rank(conn, _unit(0))
    assert hit.fmt == "pdf" and hit.scope == "omnibus"
    assert hit.section_name == "content" and hit.is_table is False
    conn.close()


def test_html_rows_carry_no_page_citation():
    """Every html row has page_start=1 because the document is one page, so
    rendering "p.1" would invent a citation the source does not have."""
    conn = _connect(":memory:")
    conn.execute(
        "INSERT INTO chunks (id, doc_key, text, payer, payer_name, drug,"
        " brand_name, plan_type, page_start, page_end, chunk_index, doc_url,"
        " fmt, scope, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("h", "doc", "t", "aetna", "Aetna", "d", "B", "commercial", 1, 1, 0,
         "https://example.test/x.html", "html", "dedicated", _unit(0).tobytes()))
    _insert(conn, [("p", "aetna", "d", "B", "dedicated", "t2", _unit(1))])
    by_fmt = {h.fmt: h for h in _rank(conn, _unit(0) + _unit(1))}
    assert by_fmt["html"].page_label is None
    assert by_fmt["pdf"].page_label == "p.1"
    conn.close()


def test_cli_prints_no_pagination_for_an_html_source(capsys, monkeypatch):
    from docsearch import store
    from docsearch.store import Hit

    def fake(*a, **k):
        base = dict(text="chunk body", score=0.5, payer="aetna",
                    payer_name="Aetna", drug="d", brand_name="B",
                    plan_type="commercial", page_start=1, page_end=1,
                    doc_url="https://example.test/x")
        return [Hit(**base, fmt="pdf", page_label="p.1"),
                Hit(**base, fmt="html", page_label=None)]

    monkeypatch.setattr(store, "search", fake)
    monkeypatch.setattr("sys.argv", ["store", "criteria"])
    store.main()
    out = capsys.readouterr().out
    assert "p.1" in out and "no pagination" in out


# --- embed ------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _FakeClient:
    """Records what embed.py actually puts on the wire."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json):
        self.calls.append(json)
        return self.responses.pop(0)


def _patch_client(monkeypatch, responses):
    from docsearch import embed
    client = _FakeClient(responses)
    monkeypatch.setattr(embed.httpx, "Client", lambda **k: client)
    return client


def test_embed_sends_the_contract_body_and_batches_at_32(monkeypatch):
    from docsearch import embed
    vec = [0.0] * DIM
    client = _patch_client(monkeypatch, [
        _FakeResponse(200, {"embeddings": [vec] * 32}),
        _FakeResponse(200, {"embeddings": [vec] * 3}),
    ])
    out = embed.embed_docs([f"doc {i}" for i in range(35)])
    assert len(out) == 35
    assert [len(c["input"]) for c in client.calls] == [32, 3]
    body = client.calls[0]
    # truncate False turns silent truncation into a catchable 400, and
    # keep_alive -1 is what stops the 5-minute unload and its 8 s reload.
    assert body["truncate"] is False and body["keep_alive"] == -1
    assert body["model"] == embed.MODEL
    assert body["input"][0].startswith("title: none | text: ")


def test_embed_query_uses_the_query_prefix_not_the_document_one(monkeypatch):
    from docsearch import embed
    client = _patch_client(
        monkeypatch, [_FakeResponse(200, {"embeddings": [[0.0] * DIM]})])
    embed.embed_query("does the patient need ECOG 0-1")
    assert client.calls[0]["input"] == [
        "task: search result | query: does the patient need ECOG 0-1"]


@pytest.mark.parametrize("status", [400, 404, 500, 302])
def test_embed_raises_rather_than_returning_an_empty_list(monkeypatch, status):
    """The one place the return-a-safe-value convention is harmful: an empty
    return truncates zip(texts, vectors) and drops the document silently.
    302 is included because is_error would let a redirect through to .json()."""
    from docsearch import embed
    _patch_client(monkeypatch, [_FakeResponse(
        status, text='{"error":"the input length exceeds the context length"}')])
    with pytest.raises(RuntimeError, match=r"\[embed error\]"):
        embed.embed_docs(["x"])


# --- serve ------------------------------------------------------------------

def test_search_endpoint_is_sync_and_returns_the_contract_shape(monkeypatch):
    """A sync def is what lets Starlette run this in a threadpool. An async def
    would block the event loop on every query, which is silent until load."""
    import inspect

    from fastapi.testclient import TestClient

    from docsearch import serve
    from docsearch.store import Hit

    assert not inspect.iscoroutinefunction(serve.search_endpoint)

    monkeypatch.setattr(serve, "search", lambda *a, **k: [Hit(
        text="t", score=0.5, payer="aetna", payer_name="Aetna", drug="d",
        brand_name="B", plan_type="commercial", page_start=1, page_end=1,
        doc_url="https://example.test/x.pdf")])
    body = TestClient(serve.app).get("/search?q=x").json()
    assert body["hits"][0]["fmt"] == "pdf"
    assert body["hits"][0]["scope"] == "dedicated"
    assert body["hits"][0]["page_start"] == 1


def test_search_endpoint_rejects_an_out_of_range_top_k(monkeypatch):
    from fastapi.testclient import TestClient

    from docsearch import serve

    monkeypatch.setattr(serve, "search", lambda *a, **k: [])
    client = TestClient(serve.app)
    assert client.get("/search?q=x&top_k=-1").status_code == 422
    assert client.get("/search?q=x&top_k=100000").status_code == 422
    assert client.get("/search?q=x&top_k=8").status_code == 200


def test_search_endpoint_rejects_an_empty_query(monkeypatch):
    """Silence transcribed by the voice agent must not return citations."""
    from fastapi.testclient import TestClient

    from docsearch import serve

    monkeypatch.setattr(serve, "search", lambda *a, **k: [])
    client = TestClient(serve.app)
    assert client.get("/search?q=").status_code == 422
    assert client.get("/search?q=%20%20").status_code == 422
    assert client.get("/search?q=x").status_code == 200
