# Backend

`docsearch.serve` now hosts two independent local APIs:

- the original policy-document search experiment at `GET /search`; and
- the outbound demo-call API at `POST /calls` and `GET /calls/{call_id}`.

The call API does not query the current `docsearch` schema or require Ollama.
Its criteria lookup is a separate read-only `CriteriaRepository` boundary for
the teammate-owned payer-criteria SQLite database.

## Run the outbound backend

```bash
cp .env.example .env.local
# Fill Tross credentials and use `lk --project gemma4hackathon app env ...`
uv sync
uv run python -m docsearch.serve
```

It binds to `127.0.0.1:8000` by default. The API is intentionally unauthenticated
for the local hackathon demo, so do not expose that bind address to a network.

Check readiness:

```bash
curl http://127.0.0.1:8000/health
```

The response includes the call service's criteria-repository readiness. A
missing or empty criteria database is not a reason to block a call: the worker
uses restricted public Tavily research as its fallback.

Launch a synthetic demo call:

```bash
curl --request POST http://127.0.0.1:8000/calls \
  --header 'content-type: application/json' \
  --data '{
    "to_phone_number": "+12125550123",
    "patient_id": "sandbox-patient-id",
    "payer": "aetna",
    "plan_type": "commercial",
    "drug": "pembrolizumab"
  }'
```

The API returns `202` immediately. Poll the supplied `status_url`. It returns a
masked destination, lifecycle state, outcome, sanitized summary, unresolved
questions, and public sources; it never returns the full Tross payload or a
transcript. Only one non-terminal call is allowed at a time.

## Tross-free synthetic phone test

`scripts/demo_call.py` runs the same LiveKit dispatch, worker context/callback,
and SIP path as the normal backend, but supplies a fixed synthetic patient brief
instead of contacting Tross. It is intentionally separate from
`python -m docsearch.serve`, so the regular backend retains its fail-before-dial
Tross behavior.

Use only a Twilio-verified demo number you are authorized to call. First start
the synthetic backend; it binds only to `127.0.0.1:8000` and allowlists the one
destination supplied on the command line. It needs only the three LiveKit
variables in `be/.env.local`, not any `TROSS_*` values.

```bash
cd be
uv run python scripts/demo_call.py serve --allow-to +13478868173
```

In another terminal, start the worker. Its `voice/.env.local` still needs the
LiveKit, SIP trunk, Cerebras, Deepgram, Cartesia, and Tavily configuration.

```bash
cd voice
lk agent dev
```

Then launch and poll the synthetic call from a third terminal. `--confirm` is
required before the script sends a request that can dial the allowlisted number.

```bash
cd be
uv run python scripts/demo_call.py launch \
  --to +13478868173 \
  --payer aetna \
  --plan-type commercial \
  --drug pembrolizumab \
  --confirm
```

Keep the `serve` process running until the launcher reports a terminal status;
the worker needs it for the capability-protected context and result callback.
Do not run the regular backend on port 8000 at the same time.

## Environment

See [`.env.example`](.env.example). `PAYER_CRITERIA_DB_PATH` is optional until
the teammate-owned schema is available. Do not create, migrate, copy, or infer
tables for that database here. Once it arrives, implement its parameterized
read-only mapping entirely inside the dedicated SQLite adapter.

## Original document-search experiment

The original index remains available independently:

```bash
ollama pull embeddinggemma
uv run python -m docsearch.index
curl 'http://127.0.0.1:8000/search?q=coverage+criteria&payer=aetna'
```

It is not a dependency of the outbound-call runbook.

## Tests

```bash
uv run pytest
```
