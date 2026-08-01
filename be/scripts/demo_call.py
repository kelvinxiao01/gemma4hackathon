"""Run a Tross-free synthetic backend and launch one allowlisted demo call.

Start ``serve`` in one terminal, the LiveKit worker in another, then use
``launch --confirm`` from a third terminal.  The server must stay running for
the duration of the call because the worker fetches context and posts callbacks
through its token-protected localhost routes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from typing import Sequence

import httpx
import uvicorn
from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from calling.models import CallStatusResponse, TERMINAL_STATUSES
from calling.synthetic_demo import (
    DialConfirmationRequired,
    SyntheticCallOptions,
    SyntheticDemoConfigurationError,
    build_synthetic_call_request,
    build_synthetic_demo_app,
    build_synthetic_demo_services,
    is_synthetic_demo_health,
    require_dial_confirmation,
    required_livekit_config,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite value greater than zero")
    return parsed


def _call_options(args: argparse.Namespace) -> SyntheticCallOptions:
    return SyntheticCallOptions(
        to_phone_number=args.to_phone_number,
        payer=args.payer,
        plan_type=args.plan_type,
        drug=args.drug,
    )


def _validated_destination(number: str) -> str:
    return build_synthetic_call_request(SyntheticCallOptions(
        to_phone_number=number,
        payer="aetna",
        plan_type="commercial",
        drug="pembrolizumab",
    )).to_phone_number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run an explicitly allowlisted synthetic demo call without Tross. "
            "The real LiveKit worker and SIP trunk are still used."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser(
        "serve",
        help="serve the synthetic local backend required by the voice worker",
    )
    serve.add_argument(
        "--allow-to",
        dest="allow_to",
        required=True,
        help="the one US E.164 destination this temporary backend may dial",
    )

    launch = commands.add_parser(
        "launch",
        help="create and monitor one synthetic demo call",
    )
    launch.add_argument("--to", dest="to_phone_number", required=True)
    launch.add_argument(
        "--payer",
        choices=("aetna", "anthem", "cigna", "uhc"),
        default="aetna",
    )
    launch.add_argument(
        "--plan-type",
        choices=("commercial", "medicare", "medicaid"),
        default="commercial",
    )
    launch.add_argument("--drug", default="pembrolizumab")
    launch.add_argument("--poll-interval", type=_positive_float, default=1.0)
    launch.add_argument(
        "--confirm",
        action="store_true",
        help="required acknowledgement before this command asks the backend to dial",
    )
    return parser


def _load_backend_environment() -> None:
    # The server needs only LiveKit credentials.  Tross configuration is not
    # read or checked by this synthetic harness.
    load_dotenv(BACKEND_ROOT / ".env.local", override=False)


def _serve(args: argparse.Namespace) -> None:
    _load_backend_environment()
    livekit_url, api_key, api_secret = required_livekit_config(os.environ)
    allowed_destination = _validated_destination(args.allow_to)

    services = build_synthetic_demo_services(
        livekit_url=livekit_url,
        api_key=api_key,
        api_secret=api_secret,
        allowed_destination=allowed_destination,
    )
    app = build_synthetic_demo_app(services)
    print(
        "Synthetic backend listening at "
        f"http://{DEFAULT_HOST}:{DEFAULT_PORT}; destination allowlist ends in "
        f"{allowed_destination[-4:]}. Keep this process running until the call "
        "reaches a terminal status.",
        flush=True,
    )
    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT)


async def _launch(args: argparse.Namespace) -> CallStatusResponse:
    require_dial_confirmation(args.confirm)
    request = build_synthetic_call_request(_call_options(args))
    base_url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        health = await client.get("/health")
        if (health.status_code != 200
                or not is_synthetic_demo_health(_response_json(health))):
            raise RuntimeError(
                "refusing to call: the local backend is not the synthetic demo "
                "server; start `demo_call.py serve` first")
        response = await client.post(
            "/calls",
            json=request.model_dump(mode="json"),
        )
        if response.status_code != 202:
            raise RuntimeError(_request_error(response))
        accepted = response.json()
        print(
            f"Call accepted: {accepted['call_id']} "
            f"({accepted['status']}). Polling {accepted['status_url']}",
            flush=True,
        )

        previous: tuple[str, str | None] | None = None
        while True:
            status_response = await client.get(accepted["status_url"])
            if status_response.status_code != 200:
                raise RuntimeError(_request_error(status_response))
            current = CallStatusResponse.model_validate(status_response.json())
            state = (current.status.value,
                     current.outcome.value if current.outcome else None)
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
                    + json.dumps(current.model_dump(mode="json"), separators=(",", ":")),
                    flush=True,
                )
                return current
            await asyncio.sleep(args.poll_interval)


def _request_error(response: httpx.Response) -> str:
    payload = _response_json(response)
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return f"backend request failed with HTTP {response.status_code}: {detail or 'unknown error'}"


def _response_json(response: httpx.Response) -> object | None:
    try:
        return response.json()
    except ValueError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            _serve(args)
        else:
            asyncio.run(_launch(args))
    except (
        DialConfirmationRequired,
        SyntheticDemoConfigurationError,
        httpx.HTTPError,
        RuntimeError,
        ValueError,
    ) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
