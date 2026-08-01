"""Local-only HTTP interface for outbound coverage calls."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status

from .coordinator import (
    ActiveCallError,
    CallCoordinator,
    CallNotFoundError,
    ContextNotReadyError,
    InvalidCapabilityError,
    InvalidStateTransitionError,
)
from .criteria import (
    CriteriaRepository,
    UnavailableCriteriaRepository,
    repository_health,
)
from .livekit import LiveKitDispatcher
from .models import (
    AgentEvent,
    CallAccepted,
    CallRequest,
    CallStatusResponse,
    InternalContextResponse,
)
from .tross import TrossClient


router = APIRouter(tags=["calls"])


@dataclass(slots=True)
class CallingServices:
    coordinator: CallCoordinator
    criteria_repository: CriteriaRepository


def build_calling_services() -> CallingServices:
    """Construct runtime dependencies without contacting any remote service."""

    # Resolve relative to be/ rather than the process working directory.
    # `override=False` preserves deliberately injected environment values in
    # deployment and tests.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env.local",
                override=False)
    # Keep the eventual configuration seam visible without reading, creating,
    # or guessing anything about the teammate-owned SQLite schema.
    criteria_repository = UnavailableCriteriaRepository(
        readiness_mode=("awaiting-schema" if os.getenv("PAYER_CRITERIA_DB_PATH")
                        else "unavailable"),
    )
    coordinator = CallCoordinator(
        tross=TrossClient(
            api_key=os.getenv("TROSS_API_KEY"),
            org_id=os.getenv("TROSS_ORG_ID"),
            auth_id=os.getenv("TROSS_AUTH_ID"),
        ),
        criteria_repository=criteria_repository,
        dispatcher=LiveKitDispatcher(
            url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        ),
    )
    return CallingServices(
        coordinator=coordinator,
        criteria_repository=criteria_repository,
    )


def install_calling_router(
    app: FastAPI,
    *,
    services: CallingServices | None = None,
) -> None:
    """Install once and allow tests/local composition to replace dependencies."""

    if not getattr(app.state, "calling_router_installed", False):
        app.include_router(router)
        app.state.calling_router_installed = True
    app.state.calling_services = services or build_calling_services()


def calling_health(app: FastAPI) -> dict[str, object]:
    services = _services_from_app(app)
    return {"criteria_repository": repository_health(services.criteria_repository)}


def _services_from_app(app: FastAPI) -> CallingServices:
    services = getattr(app.state, "calling_services", None)
    if services is None:
        services = build_calling_services()
        app.state.calling_services = services
    return services


def get_calling_services(request: Request) -> CallingServices:
    return _services_from_app(request.app)


@router.post(
    "/calls",
    response_model=CallAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_call(
    call: CallRequest,
    request: Request,
    services: CallingServices = Depends(get_calling_services),
) -> CallAccepted:
    try:
        record = await services.coordinator.create_call(call)
    except ActiveCallError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an outbound call is already active",
        ) from exc
    return CallAccepted(
        call_id=record.call_id,
        status=record.status,
        status_url=str(request.url_for("get_call", call_id=record.call_id)),
    )


@router.get("/calls/{call_id}", response_model=CallStatusResponse, name="get_call")
async def get_call(
    call_id: str,
    services: CallingServices = Depends(get_calling_services),
) -> CallStatusResponse:
    try:
        return await services.coordinator.public_status(call_id)
    except CallNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="call not found") from exc


@router.get(
    "/internal/calls/{call_id}/context",
    response_model=InternalContextResponse,
    include_in_schema=False,
)
async def get_internal_context(
    call_id: str,
    x_call_context_token: str | None = Header(
        default=None, alias="X-Call-Context-Token"),
    services: CallingServices = Depends(get_calling_services),
) -> InternalContextResponse:
    try:
        return await services.coordinator.internal_context(
            call_id, x_call_context_token)
    except InvalidCapabilityError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="invalid call capability") from exc
    except ContextNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="call context is not ready") from exc
    except CallNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="call not found") from exc


@router.post(
    "/internal/calls/{call_id}/events",
    response_model=CallStatusResponse,
    include_in_schema=False,
)
async def post_agent_event(
    call_id: str,
    event: AgentEvent,
    x_call_context_token: str | None = Header(
        default=None, alias="X-Call-Context-Token"),
    services: CallingServices = Depends(get_calling_services),
) -> CallStatusResponse:
    try:
        return await services.coordinator.apply_agent_event(
            call_id, x_call_context_token, event)
    except InvalidCapabilityError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="invalid call capability") from exc
    except CallNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="call not found") from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="invalid call state transition") from exc
