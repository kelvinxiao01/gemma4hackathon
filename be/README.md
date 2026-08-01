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
# Fill LiveKit credentials with `lk --project gemma4hackathon app env ...`
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
    "to_phone_number": "+13478868173",
    "patient_id": "case-002",
    "payer": "aetna",
    "plan_type": "commercial",
    "drug": "ustekinumab"
  }'
```

The API returns `202` immediately. Poll the supplied `status_url`. It returns a
masked destination, lifecycle state, outcome, sanitized summary, unresolved
questions, and public sources; it never returns the full synthetic-case payload or a
transcript. Only one non-terminal call is allowed at a time.

## Find an infusion center near ZIP 10001 and run the demo call

Facility discovery is a separate, location-only flow. It sends Tavily a fixed
ZIP `10001` query, returns at most three candidates with a normalized US phone
number and source URL, and retains the selection in memory for ten minutes. It
does not send any synthetic case, patient, provider, or caller data to Tavily.

Add the same `TAVILY_API_KEY` used by the worker and your verified demo number
to `be/.env.local`, then run the normal backend (not
`scripts/demo_call.py serve`):

```dotenv
TAVILY_API_KEY=...
DEMO_OUTBOUND_PHONE_NUMBER=+13478868173
```

```bash
cd be
uv sync
uv run python -m docsearch.serve
```

In another terminal, inspect the candidates. This hackathon flow supports only
ZIP `10001`:

```bash
cd be
uv run python scripts/facility_call.py discover --zip 10001
```

`discover` prints an opaque `search_id` and a `candidate_id` for each exact
candidate snapshot. Independently verify its phone number on an official or
otherwise authoritative source, then select one of those IDs and explicitly
acknowledge the demo call. The `case_id` derives the call's payer, plan, and
drug from `fixtures/cases.json`; it is not included in the Tavily query.

```bash
cd be
uv run python scripts/facility_call.py launch \
  --search-id '<search_id printed by discover>' \
  --candidate-id '<candidate_id printed by discover>' \
  --case-id case-002 \
  --confirm
```

The script uses the normal local backend's `POST /facility-searches`,
`GET /facility-searches/{search_id}`, and
`POST /facility-searches/{search_id}/demo-calls`. The second route shows the
exact short-lived snapshot you selected; the third accepts only a candidate
returned by it and requires `confirm_destination: true`. It never accepts a
free-form phone number.

The selected facility is **never dialed**. It is private context for the
Conduit demo agent; every facility demo call is sent only to
`DEMO_OUTBOUND_PHONE_NUMBER`. Discovery is restricted to ZIP `10001`. A
candidate phone is deterministically extracted only when the Tavily result
labels it as a phone/call number; it is not guessed or independently verified
as a direct line. Tavily ranking is location-relevant search ranking, not a
calculated geospatial distance, so treat candidates as display-only and verify
them separately before any future real-world use.

## Synthetic cases

`patient_id` is a fixture `case_id`. Its payer, plan type, and drug must match
the selected case; a mismatch fails before dispatch. The launcher derives those
three fields automatically.

| Case | Coverage scenario |
| --- | --- |
| `case-001` | Aetna commercial ustekinumab; no TB screening recorded |
| `case-002` | Aetna commercial ustekinumab; negative TB screening recorded |
| `case-003` | UHC commercial pembrolizumab; no matching committed policy |

## Fixture-backed synthetic phone test

`scripts/demo_call.py` runs the same LiveKit dispatch, worker context/callback,
and SIP path as the normal backend, using a selected case from
`fixtures/cases.json`. It is intentionally separate from
`python -m docsearch.serve` because it also allowlists one destination for the
phone test.

Use only a Twilio-verified demo number you are authorized to call. First start
the synthetic backend; it binds only to `127.0.0.1:8000` and allowlists the one
destination supplied on the command line. It needs only the three LiveKit
variables in `be/.env.local`.

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
  --case-id case-002 \
  --confirm
```

Keep the `serve` process running until the launcher reports a terminal status;
the worker needs it for the capability-protected context and result callback.
Do not run the regular backend on port 8000 at the same time.

## Environment

See [`.env.example`](.env.example). LiveKit settings are needed for calls;
`TAVILY_API_KEY` is additionally required for facility discovery.
`PAYER_CRITERIA_DB_PATH` is optional until
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
