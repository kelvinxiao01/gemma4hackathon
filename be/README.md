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
    "to_phone_number": "+12125550123",
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

## Find and call an infusion center near ZIP 10001

Facility discovery is a separate, location-only flow. It sends Tavily a fixed
query based on the supplied five-digit ZIP, returns at most three candidates
with a normalized US phone number and source URL, and retains the selection in
memory for ten minutes. It does not send any synthetic case, patient, provider,
or caller data to Tavily.

Add the same `TAVILY_API_KEY` used by the worker to `be/.env.local`, then run
the normal backend (not `scripts/demo_call.py serve`):

```bash
cd be
uv sync
uv run python -m docsearch.serve
```

In another terminal, inspect the candidates. The current hackathon default is
ZIP `10001`:

```bash
cd be
uv run python scripts/facility_call.py discover --zip 10001
```

To place a call, rerun discovery, select the displayed candidate position, and
explicitly acknowledge the destination. The `case_id` derives the call's payer,
plan, and drug from `fixtures/cases.json`; it is not included in the Tavily
query.

```bash
cd be
uv run python scripts/facility_call.py launch \
  --zip 10001 \
  --candidate 1 \
  --case-id case-002 \
  --confirm
```

The script uses the normal local backend's `POST /facility-searches` followed
by `POST /facility-searches/{search_id}/calls`. The second route accepts only a
candidate returned by that short-lived search and requires
`confirm_destination: true`; it never accepts a free-form phone number. Tavily
ranking is location-relevant search ranking, not a calculated geospatial
distance, so verify the shown source before confirming a call.

Twilio Trial accounts cannot call an arbitrary discovered center: Twilio must
already recognize the destination as verified. Upgrade the account before
calling real facilities, or continue using the single verified demo number and
the fixture-backed harness below. The existing `demo_call.py serve` allowlist
remains intentionally unable to dial a discovered number.

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
