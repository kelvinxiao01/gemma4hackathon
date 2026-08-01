# Prior-Auth Copilot

A prior authorization copilot that runs locally on the specialist's machine.

Built for Build with Gemma NYC: On-Device AI for Healthcare. Track 2, Agentic Care Copilots.

## The problem

Prior authorization is manual administrative work. For a single oncology request, a specialist locates the payer's coverage policy, reads the criteria out of a long PDF, checks the patient's chart against each one, and drafts a justification letter. Over 70% of prior-auth volume still moves by fax, and CMS-0057-F requires electronic prior authorization by 2026.

The users are billing and prior-authorization specialists, not clinicians.

## Why local inference

A cloud version of this tool needs placeholder tokens, schema allowlists, and PHI screening on both directions of every request. Those layers exist because inference runs on a remote server.

Gemma 4 runs on the specialist's machine instead, so patient context never leaves it and there is no PHI screening layer to build.

## Gemma 4 in this project

Every inference call goes to a Gemma-family model. No OpenAI, Anthropic, or other LLM client appears in the dependency tree.

| Model | Role | Location |
|---|---|---|
| EmbeddingGemma (768-dim) | Embeds policy chunks at index time and queries at search time. | `be/docsearch/embed.py` |
| Gemma 4 (`gemma4:e4b`) | Reasoning over retrieved policy text: criteria checking, structured output, letter drafting. | `be/` agent layer, `voice/` |

Both run through [Ollama](https://ollama.com). No API key is required; `GEMMA_PROVIDER=ollama` is the default.

Retrieval is embedding-based rather than keyword matching. Policy documents are chunked within page boundaries and embedded using EmbeddingGemma's asymmetric task prefixes (`title: none | text: ...` for documents, `task: search result | query: ...` for queries). Ollama returns L2-normalised vectors, so cosine similarity reduces to a dot product over a small matrix. Search is exact and needs no vector database.

## Architecture

```
be/       Python. Policy corpus, indexing, retrieval, HTTP search API.
fe/       Next.js. Specialist-facing UI.
voice/    LiveKit voice agent. Calls the retrieval API over HTTP.
```

`be/` exposes one contract that the other components consume:

```
GET /search?q=<query>&payer=<slug>&drug=<slug>&top_k=8
```

Each hit carries the chunk text, similarity score, payer, drug, plan type, page number, and source URL, so any answer can cite the page it came from.

## Design decisions

- Answers cite their policy source. If a criterion is absent from the document, the response is "not found in policy" rather than an inference.
- `plan_type` (commercial, Medicare, Medicaid) appears on every citation, because coverage criteria differ across them.
- Confidence is explicit and carries a reason code.
- A human makes every decision. Several states prohibit automated approval or denial of prior-authorization requests.

## Data

Public payer coverage policies only: published clinical policy bulletins and coverage criteria from Aetna, Anthem, Cigna, and UnitedHealthcare. No patient data is in this repository. Patient context used in demos is synthetic.

## Running it

Requires [Ollama](https://ollama.com) and [uv](https://docs.astral.sh/uv/).

```bash
ollama pull embeddinggemma        # 622 MB, retrieval
ollama pull gemma4:e4b            # 9.6 GB, reasoning

cd be
uv sync
uv run python -m docsearch.index  # builds the index from the committed corpus, no network
uv run python -m docsearch.serve  # http://127.0.0.1:8000
```

Query it directly:

```bash
curl "http://127.0.0.1:8000/search?q=pembrolizumab+initial+approval+criteria&payer=aetna"
```

The policy corpus is committed, so indexing needs no network access and reproduces on any machine.

## Scope

Decision-support software for administrative staff. It does not diagnose, recommend treatment, or make approval decisions. It locates the applicable policy language, shows where that language came from, and drafts text for a human to review.

## License

MIT. See [LICENSE](LICENSE).
