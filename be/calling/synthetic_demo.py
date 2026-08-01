"""Fixture-backed composition helpers for one explicitly allowlisted demo call.

This module is deliberately separate from the normal backend wiring.  It
preserves the actual dispatch, capability-token, callback, and SIP flow while
using a committed synthetic case instead of an external patient lookup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from .coordinator import CallCoordinator
from .criteria import UnavailableCriteriaRepository
from .livekit import AgentDispatcher, LiveKitDispatcher
from .models import CallRequest
from .router import CallingServices, calling_health, install_calling_router
from .synthetic_cases import SyntheticCasePatientSource


class SyntheticDemoConfigurationError(RuntimeError):
    """Raised before a local demo server starts with incomplete credentials."""


class DialConfirmationRequired(RuntimeError):
    """Raised when the launcher was not explicitly approved to dial."""


@dataclass(frozen=True, slots=True)
class SyntheticCallOptions:
    """The selected fixture case and destination used for a synthetic call."""

    to_phone_number: str
    case_id: str


def build_synthetic_call_request(options: SyntheticCallOptions) -> CallRequest:
    """Derive a valid public request from one committed synthetic case."""

    return SyntheticCasePatientSource().build_call_request(
        to_phone_number=options.to_phone_number,
        case_id=options.case_id,
    )


def required_livekit_config(
    environment: Mapping[str, str | None],
) -> tuple[str, str, str]:
    """Read only the credentials needed for dispatch; no patient API is used."""

    livekit_url = (environment.get("LIVEKIT_URL") or "").strip()
    api_key = (environment.get("LIVEKIT_API_KEY") or "").strip()
    api_secret = (environment.get("LIVEKIT_API_SECRET") or "").strip()
    configured = {
        "LIVEKIT_URL": livekit_url,
        "LIVEKIT_API_KEY": api_key,
        "LIVEKIT_API_SECRET": api_secret,
    }
    missing = [key for key, value in configured.items() if not value]
    if missing:
        raise SyntheticDemoConfigurationError(
            "missing required LiveKit configuration: " + ", ".join(missing))
    return livekit_url, api_key, api_secret


def require_dial_confirmation(confirmed: bool) -> None:
    """Require an intentional CLI flag before a launcher can create a call."""

    if not confirmed:
        raise DialConfirmationRequired(
            "refusing to dial without --confirm; no request was sent")


def is_synthetic_demo_health(payload: object) -> bool:
    """Recognize only the marker emitted by the isolated synthetic server."""

    return isinstance(payload, dict) and payload.get("mode") == "synthetic-demo"


def build_synthetic_demo_services(
    *,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    allowed_destination: str,
    dispatcher: AgentDispatcher | None = None,
    task_spawner: Callable[[Coroutine[Any, Any, Any]], object] = asyncio.create_task,
    timeout_seconds: float = 300,
) -> CallingServices:
    """Build the real coordinator with its fixture-backed patient source."""

    criteria_repository = UnavailableCriteriaRepository(readiness_mode="synthetic-demo")
    coordinator = CallCoordinator(
        patient_source=SyntheticCasePatientSource(),
        criteria_repository=criteria_repository,
        dispatcher=dispatcher or LiveKitDispatcher(
            url=livekit_url,
            api_key=api_key,
            api_secret=api_secret,
        ),
        task_spawner=task_spawner,
        timeout_seconds=timeout_seconds,
    )
    return CallingServices(
        coordinator=coordinator,
        criteria_repository=criteria_repository,
        allowed_destination=allowed_destination,
    )


def build_synthetic_demo_app(services: CallingServices) -> FastAPI:
    """Expose the same local call and protected worker-callback routes."""

    if not services.allowed_destination:
        raise SyntheticDemoConfigurationError(
            "a synthetic demo backend requires one allowlisted destination")
    app = FastAPI(title="Synthetic outbound-call harness")
    install_calling_router(app, services=services)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "mode": "synthetic-demo",
            "calling": calling_health(app),
        }

    return app
