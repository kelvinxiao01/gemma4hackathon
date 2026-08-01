"""Run the Mac-side relay for a backend hosted in a Daytona sandbox.

The process deliberately listens only on loopback.  It polls the Daytona
backend for minimal LiveKit work and proxies the local worker's existing
capability-protected context and event endpoints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from calling.daytona_relay import (
    DaytonaRelay,
    DaytonaRelayConfigurationError,
    build_daytona_relay_app,
    relay_configuration_from_environment,
)
from calling.livekit import LiveKitDispatcher

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a port number") from exc
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the loopback-only Daytona backend / LiveKit relay."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.host != DEFAULT_HOST:
        raise SystemExit("the relay may listen only on 127.0.0.1")
    load_dotenv(BACKEND_ROOT / ".env.local", override=False)
    try:
        backend_url, relay_secret, livekit_url, api_key, api_secret = (
            relay_configuration_from_environment(os.environ)
        )
    except DaytonaRelayConfigurationError as exc:
        raise SystemExit(f"relay configuration error: {exc}") from exc

    relay = DaytonaRelay(
        backend_url=backend_url,
        relay_secret=relay_secret,
        livekit_dispatcher=LiveKitDispatcher(
            url=livekit_url,
            api_key=api_key,
            api_secret=api_secret,
        ),
    )
    print(
        f"Daytona relay listening at http://{DEFAULT_HOST}:{args.port}; "
        "the LiveKit worker must use that loopback URL.",
        flush=True,
    )
    uvicorn.run(build_daytona_relay_app(relay), host=DEFAULT_HOST, port=args.port)


if __name__ == "__main__":
    main()
