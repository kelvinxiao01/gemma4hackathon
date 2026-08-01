"""Gemma through the local Ollama server.

One seam for every generation call, so a provider change is a rewrite of this
file rather than a refactor of the callers. No API key: the model runs on the
specialist's own machine.
"""

import json
from collections.abc import Iterator

import httpx

URL = "http://localhost:11434/api/chat"
MODEL = "gemma4:e4b"

# A qualification prompt carries eight policy excerpts and a patient record.
# Ollama silently truncates the prompt at the context ceiling, and a truncated
# policy is a citation of text the model never saw.
NUM_CTX = 8192

# Generous because the ceiling is generation speed, not this number: a 500
# token answer at 25 tok/s already sits at 20 s, and the request is the only
# thing on the GPU. httpx applies this per operation rather than as a deadline,
# so it bounds a silent connection, not total wall time.
TIMEOUT = 300.0

# The readiness probe answers a dashboard, so it fails fast instead of waiting
# out a generation. Ollama serialises requests per model, so an unbounded probe
# issued during a qualification would queue behind it and report a warm model
# as unreachable.
PING_TIMEOUT = 15.0


def _body(messages: list[dict], num_predict: int, schema: dict | None) -> dict:
    body = {
        "model": MODEL,
        "messages": messages,
        # Thinking triples the token count for no measured gain here, and its
        # output would reach the user through the qualify stream.
        "think": False,
        # The model stays resident between demo runs. Ollama evicts it after
        # five idle minutes otherwise, and the reload costs about eight
        # seconds in front of the audience.
        "keep_alive": -1,
        "options": {"temperature": 0, "num_ctx": NUM_CTX,
                    "num_predict": num_predict},
    }
    if schema is not None:
        body["format"] = schema
    return body


def chat(messages: list[dict], schema: dict | None = None,
         num_predict: int = 1024, timeout: float = TIMEOUT) -> str:
    """One non-streaming completion. `schema` constrains the output to JSON."""
    with httpx.Client(timeout=timeout) as client:
        r = client.post(URL, json=_body(messages, num_predict, schema)
                        | {"stream": False})
        # Checked before .json(): a non-2xx body is Ollama's error text, and
        # parsing it as a completion turns a readable message into a KeyError.
        if not r.is_success:
            raise RuntimeError(f"[gemma error] {r.status_code}: {r.text[:200]}")
        payload = r.json()
    if payload.get("done_reason") != "stop":
        # "length" here means the answer was cut mid-JSON. Returning it would
        # hand the caller a half-object to parse.
        raise RuntimeError(
            f"[gemma error] incomplete generation: {payload.get('done_reason')}")
    return payload["message"]["content"]


def stream_chat(messages: list[dict],
                num_predict: int = 1024) -> Iterator[str]:
    """Yield content fragments as the model produces them."""
    # Both context managers stay open across the yields on purpose. Closing the
    # client between fragments would abort the response mid-generation.
    with httpx.Client(timeout=TIMEOUT) as client:
        with client.stream("POST", URL,
                           json=_body(messages, num_predict, None)
                           | {"stream": True}) as r:
            if not r.is_success:
                r.read()
                raise RuntimeError(
                    f"[gemma error] {r.status_code}: {r.text[:200]}")
            done_reason = None
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    yield piece
                if chunk.get("done"):
                    done_reason = chunk.get("done_reason")
    if done_reason != "stop":
        # A generator cannot raise usefully into a half-sent HTTP response, so
        # the truncation is marked in the stream instead of being swallowed.
        yield "\n[incomplete]"


def ping() -> str:
    """Readiness probe. Short enough to stay under a second when warm."""
    try:
        chat([{"role": "user", "content": "Reply with one word: ready."}],
             num_predict=8, timeout=PING_TIMEOUT)
    except RuntimeError as exc:
        # Running into the token ceiling still proves the model answered, and
        # reporting a warm model as unreachable would send the presenter
        # chasing a problem that is not there.
        if "incomplete generation" not in str(exc):
            print(f"[gemma error] health: {exc}")
            return "unreachable"
    except Exception as exc:
        print(f"[gemma error] health: {type(exc).__name__}: {exc}")
        return "unreachable"
    return "ok"
