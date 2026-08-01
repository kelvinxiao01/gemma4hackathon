"""Discover a 10001 infusion center, explicitly select it, and launch one call.

The normal local backend must already be running. Discovery results remain only
in that backend's memory, and ``launch`` creates a fresh search immediately
before choosing the requested candidate index.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from calling.models import TERMINAL_STATUSES, CallStatusResponse  # noqa: E402
from calling.synthetic_demo import (  # noqa: E402
    DialConfirmationRequired,
    require_dial_confirmation,
)

DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
DEFAULT_ZIP_CODE = "10001"


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite value greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search public web results for nearby infusion centers, then call "
            "one explicitly selected candidate."
        ),
    )
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL)
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover", help="print up to three candidates")
    discover.add_argument("--zip", dest="zip_code", default=DEFAULT_ZIP_CODE)

    launch = commands.add_parser(
        "launch",
        help="search again, select one displayed candidate, and create a call",
    )
    launch.add_argument("--zip", dest="zip_code", default=DEFAULT_ZIP_CODE)
    launch.add_argument(
        "--candidate",
        type=int,
        required=True,
        help="one-based position from the current search results",
    )
    launch.add_argument("--case-id", default="case-002")
    launch.add_argument("--poll-interval", type=_positive_float, default=1.0)
    launch.add_argument(
        "--confirm",
        action="store_true",
        help="required acknowledgement before this command asks the backend to dial",
    )
    return parser


async def _search(
    client: httpx.AsyncClient,
    *,
    zip_code: str,
) -> dict[str, Any]:
    response = await client.post("/facility-searches", json={"zip_code": zip_code})
    if response.status_code != 200:
        raise RuntimeError(_request_error(response))
    payload = _response_json(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise RuntimeError("backend returned an invalid facility-search response")
    return payload


def _print_candidates(search: dict[str, Any]) -> None:
    candidates = search["candidates"]
    if not candidates:
        print("No callable infusion-center candidates were found for this ZIP.")
        return
    print(f"Candidates for ZIP {search['zip_code']} (Tavily relevance order):")
    for index, candidate in enumerate(candidates, start=1):
        print(
            f"{index}. {candidate['name']} — {candidate['phone_e164']}\n"
            f"   {candidate['source_url']}",
            flush=True,
        )


async def _discover(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(
        base_url=args.backend_url.rstrip("/"), timeout=15.0
    ) as client:
        _print_candidates(await _search(client, zip_code=args.zip_code))


async def _launch(args: argparse.Namespace) -> CallStatusResponse:
    require_dial_confirmation(args.confirm)
    async with httpx.AsyncClient(
        base_url=args.backend_url.rstrip("/"), timeout=15.0
    ) as client:
        search = await _search(client, zip_code=args.zip_code)
        candidates = search["candidates"]
        candidate_index = args.candidate - 1
        if candidate_index < 0 or candidate_index >= len(candidates):
            raise RuntimeError(
                f"candidate must be between 1 and {len(candidates)} for this search"
            )
        candidate = candidates[candidate_index]
        print(
            f"Selected {candidate['name']} at {candidate['phone_e164']}. "
            "Creating the confirmed outbound call...",
            flush=True,
        )
        response = await client.post(
            f"/facility-searches/{search['search_id']}/calls",
            json={
                "candidate_id": candidate["candidate_id"],
                "case_id": args.case_id,
                "confirm_destination": True,
            },
        )
        if response.status_code != 202:
            raise RuntimeError(_request_error(response))
        accepted = _response_json(response)
        if not isinstance(accepted, dict) or not isinstance(
            accepted.get("status_url"), str
        ):
            raise RuntimeError("backend returned an invalid call response")
        print(
            f"Call accepted: {accepted['call_id']} ({accepted['status']}). "
            f"Polling {accepted['status_url']}",
            flush=True,
        )

        previous: tuple[str, str | None] | None = None
        while True:
            status_response = await client.get(accepted["status_url"])
            if status_response.status_code != 200:
                raise RuntimeError(_request_error(status_response))
            current = CallStatusResponse.model_validate(status_response.json())
            state = (
                current.status.value,
                current.outcome.value if current.outcome else None,
            )
            if state != previous:
                print(
                    f"status={state[0]} outcome={state[1] or 'pending'} "
                    f"destination={current.destination_masked}",
                    flush=True,
                )
                previous = state
            if current.status in TERMINAL_STATUSES:
                print(
                    "Final public result: "
                    + json.dumps(
                        current.model_dump(mode="json"), separators=(",", ":")
                    ),
                    flush=True,
                )
                return current
            await asyncio.sleep(args.poll_interval)


def _response_json(response: httpx.Response) -> object | None:
    try:
        return response.json()
    except ValueError:
        return None


def _request_error(response: httpx.Response) -> str:
    payload = _response_json(response)
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return (
        f"backend request failed with HTTP {response.status_code}: "
        f"{detail or 'unknown error'}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "discover":
            asyncio.run(_discover(args))
        else:
            asyncio.run(_launch(args))
    except (DialConfirmationRequired, httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
