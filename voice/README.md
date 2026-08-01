# Outbound Coverage Voice Worker

This worker accepts only the `voice` agent dispatch created by the local backend.
It fetches per-call context through a capability-token-protected local endpoint,
dials through a LiveKit outbound SIP trunk, waits for answering-machine
detection, and then runs a focused coverage-criteria conversation.

It uses direct provider integrations:

- Cerebras `gemma-4-31b` for inference and AMD;
- Deepgram Nova-3 (`en-US`) for speech-to-text;
- Cartesia Sonic 3.5 and the existing configured voice for text-to-speech.

Tavily is available only as a bounded fallback tool: basic search, a maximum of
three results, a short timeout, no retry, and domains limited to the selected
payer's official sites plus `cms.gov`. Patient-case data is never included
in a Tavily query.

## Setup

```bash
cp .env.example .env.local
lk --project gemma4hackathon app env --write --destination .env.local .
```

Merge provider values from the existing `.env` into `.env.local`, then set
`TAVILY_API_KEY`, `SIP_OUTBOUND_TRUNK_ID`, and `CALL_BACKEND_URL`. See the root
[runbook](../README.md#twilio--livekit-setup) for the Twilio termination domain,
digest credentials, verified Trial destinations, and LiveKit trunk command.

Install and run after the backend is listening:

```bash
uv sync
lk agent dev src/agent.py
```

Run this command from the `voice/` directory so the worker loads
`voice/.env.local`. Do not start it from `voice/src/`.

`console` mode is intentionally unsupported: this is an outbound-only worker.

## Privacy and observability

The worker starts LiveKit sessions with transcript-only recording. Audio,
traces, and logs are disabled for LiveKit observability. It posts only a
structured final result to the local backend; it does not persist a transcript
or patient payload locally.

## Tests

```bash
uv run pytest
```

Tests are offline unit tests. Before a live demo, manually validate the human,
voicemail, IVR, silence, and unavailable-mailbox AMD branches with verified
demo numbers.
