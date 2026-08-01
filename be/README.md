# be

Policy corpus, retrieval index, and the HTTP search API.

## Setup

```bash
ollama pull embeddinggemma   # 622 MB
uv sync
```

## Build the index

```bash
uv run python -m docsearch.index    # ~18 s, reads the committed corpus, no network
uv run python -m docsearch.verify   # exits non-zero if anything is off
```

`index` is idempotent. Running it twice leaves the same 528 chunks, not 1056. If the schema changes, delete `data/docsearch.db` first: the table is created with `IF NOT EXISTS` and does not migrate.

## Query

```bash
uv run python -m docsearch.serve                       # 127.0.0.1:8000
curl "http://127.0.0.1:8000/search?q=ustekinumab+initial+approval+criteria"
curl "http://127.0.0.1:8000/health"
```

Or from the CLI, skipping the server:

```bash
uv run python -m docsearch.store "step therapy before nivolumab" --payer anthem
```

## Layout

```
corpus/                   committed, page-structured JSON, 15 payer documents
                          indexing keeps the operative policy and drops the
                          trailing background and revision-history sections
scripts/fetch_corpus.py   produced corpus/, run once
docsearch/embed.py        EmbeddingGemma through Ollama, the only inference call here
docsearch/index.py        corpus -> chunks -> vectors -> sqlite
docsearch/store.py        search() and the Hit record
docsearch/serve.py        FastAPI GET /search and /health
docsearch/verify.py       corpus-wide health check
data/                     gitignored, holds docsearch.db
```

## Notes for consumers

- `score` is a raw cosine. Observed range on this corpus is 0.37 to 0.72, median 0.56. It is not calibrated across queries, so fit any threshold against real output first.
- A score cannot tell you whether the answer is in the corpus. Questions whose answer is provably absent score 0.52 to 0.61, inside the range of questions that are answered correctly. Ground "not found in policy" in what the retrieved text says, not in the score.
- Render `page_label`, not `page_start`. It is `null` for html sources, which have no pagination, and you should cite `doc_url` instead. `page_start` is 1 on every html row because the document is a single page, so printing it would invent a citation. 126 of 528 rows are html.
- `scope == "omnibus"` means the `drug` field describes the document, not that chunk. Three of the 15 documents are multi-drug policies.
- `plan_type` is commercial, medicare, or medicaid. Criteria differ between them, so it belongs next to every citation.

## Tests

```bash
uv run pytest tests/test_smoke.py    # no database and no Ollama required
```
