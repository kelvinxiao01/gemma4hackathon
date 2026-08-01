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
from .facilities import (
    FacilityCandidateNotFoundError,
    FacilityDiscoveryUnavailableError,
    FacilitySearch,
    FacilitySearchNotFoundError,
    FacilitySearchService,
    HACKATHON_ZIP_CODE,
    TavilyFacilityDiscovery,
    UnavailableFacilityDiscovery,
)
from .livekit import LiveKitDispatcher
from .models import (
    AgentEvent,
    CallAccepted,
    CallRecipient,
    CallRequest,
    CallStatusResponse,
    FacilityCallRequest,
    FacilityCandidateResponse,
    FacilitySearchRequest,
    FacilitySearchResponse,
    InternalContextResponse,
    RecipientKind,
    validate_us_e164,
)
from .synthetic_cases import SyntheticCaseError, SyntheticCasePatientSource

router = APIRouter(tags=["calls"])


@dataclass(slots=True)
class CallingServices:
    coordinator: CallCoordinator
    criteria_repository: CriteriaRepository
    # A separate location-only service keeps web discovery independent of
    # synthetic case data and the coordinator's private call context.
    facility_searches: FacilitySearchService | None = None
    case_source: SyntheticCasePatientSource | None = None
    # The normal backend leaves this unset.  The synthetic local-demo harness
    # sets one destination so it cannot be repurposed to dial arbitrary numbers.
    allowed_destination: str | None = None
    # A facility selection is presentation/context only in this hackathon.
    # Facility demo calls always route to this verified demo destination.
    demo_destination: str | None = None


def build_calling_services() -> CallingServices:
    """Construct runtime dependencies without contacting any remote service."""

    # Resolve relative to be/ rather than the process working directory.
    # `override=False` preserves deliberately injected environment values in
    # deployment and tests.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env.local", override=False)
    # Keep the eventual configuration seam visible without reading, creating,
    # or guessing anything about the teammate-owned SQLite schema.
    criteria_repository = UnavailableCriteriaRepository(
        readiness_mode=(
            "awaiting-schema" if os.getenv("PAYER_CRITERIA_DB_PATH") else "unavailable"
        ),
    )
    case_source = SyntheticCasePatientSource()
    configured_demo_destination = (os.getenv("DEMO_OUTBOUND_PHONE_NUMBER") or "").strip()
    try:
        demo_destination = (
            validate_us_e164(
                configured_demo_destination,
                field_name="DEMO_OUTBOUND_PHONE_NUMBER",
            )
            if configured_demo_destination
            else None
        )
    except ValueError:
        demo_destination = None
    tavily_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    facility_discovery = (
        TavilyFacilityDiscovery(api_key=tavily_key)
        if tavily_key
        else UnavailableFacilityDiscovery()
    )
    coordinator = CallCoordinator(
        patient_source=case_source,
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
        facility_searches=FacilitySearchService(discovery=facility_discovery),
        case_source=case_source,
        demo_destination=demo_destination,
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
    facility_searches = services.facility_searches
    return {
        "criteria_repository": repository_health(services.criteria_repository),
        "facility_discovery": (
            facility_searches.health()
            if facility_searches is not None
            else {"ready": False, "mode": "unavailable"}
        ),
    }


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
    if (
        services.allowed_destination is not None
        and call.to_phone_number != services.allowed_destination
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this synthetic demo backend is limited to its configured destination",
        )
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


@router.post(
    "/facility-searches",
    response_model=FacilitySearchResponse,
)
async def create_facility_search(
    search: FacilitySearchRequest,
    services: CallingServices = Depends(get_calling_services),
) -> FacilitySearchResponse:
    """Find at most three location-only candidates; never send case data to Tavily."""

    if services.facility_searches is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="facility discovery is not configured",
        )
    if search.zip_code != HACKATHON_ZIP_CODE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"facility discovery is limited to ZIP {HACKATHON_ZIP_CODE}",
        )
    try:
        result = await services.facility_searches.search(zip_code=search.zip_code)
    except FacilityDiscoveryUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="facility discovery is unavailable",
        ) from exc
    return _facility_search_response(result)


@router.get("/facility-searches/{search_id}", response_model=FacilitySearchResponse)
async def get_facility_search(
    search_id: str,
    services: CallingServices = Depends(get_calling_services),
) -> FacilitySearchResponse:
    """Show the exact candidate snapshot a user must select before dialing."""

    if services.facility_searches is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="facility discovery is not configured",
        )
    try:
        result = await services.facility_searches.snapshot(search_id=search_id)
    except FacilitySearchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="facility search is unavailable",
        ) from exc
    return _facility_search_response(result)


@router.post(
    "/facility-searches/{search_id}/demo-calls",
    response_model=CallAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_confirmed_facility_call(
    search_id: str,
    selection: FacilityCallRequest,
    request: Request,
    services: CallingServices = Depends(get_calling_services),
) -> CallAccepted:
    """Demo-call a configured test number using a selected center as context."""

    if (
        services.facility_searches is None
        or services.case_source is None
        or services.demo_destination is None
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="facility demo calling is not configured",
        )
    try:
        candidate = await services.facility_searches.selected_candidate(
            search_id=search_id,
            candidate_id=selection.candidate_id,
        )
    except (FacilitySearchNotFoundError, FacilityCandidateNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="facility candidate is unavailable",
        ) from exc
    if (
        services.allowed_destination is not None
        and services.demo_destination != services.allowed_destination
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this synthetic demo backend is limited to its configured destination",
        )
    try:
        call = services.case_source.build_call_request(
            to_phone_number=services.demo_destination,
            case_id=selection.case_id,
        )
    except SyntheticCaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the selected synthetic case is unavailable",
        ) from exc
    try:
        record = await services.coordinator.create_call(
            call,
            recipient=CallRecipient(
                kind=RecipientKind.INFUSION_CENTER,
                name=candidate.name,
            ),
        )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="call not found"
        ) from exc


@router.get(
    "/internal/calls/{call_id}/context",
    response_model=InternalContextResponse,
    include_in_schema=False,
)
async def get_internal_context(
    call_id: str,
    x_call_context_token: str | None = Header(
        default=None, alias="X-Call-Context-Token"
    ),
    services: CallingServices = Depends(get_calling_services),
) -> InternalContextResponse:
    try:
        return await services.coordinator.internal_context(
            call_id, x_call_context_token
        )
    except InvalidCapabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid call capability"
        ) from exc
    except ContextNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="call context is not ready"
        ) from exc
    except CallNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="call not found"
        ) from exc


@router.post(
    "/internal/calls/{call_id}/events",
    response_model=CallStatusResponse,
    include_in_schema=False,
)
async def post_agent_event(
    call_id: str,
    event: AgentEvent,
    x_call_context_token: str | None = Header(
        default=None, alias="X-Call-Context-Token"
    ),
    services: CallingServices = Depends(get_calling_services),
) -> CallStatusResponse:
    try:
        return await services.coordinator.apply_agent_event(
            call_id, x_call_context_token, event
        )
    except InvalidCapabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid call capability"
        ) from exc
    except CallNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="call not found"
        ) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="invalid call state transition"
        ) from exc


def _facility_search_response(result: FacilitySearch) -> FacilitySearchResponse:
    return FacilitySearchResponse(
        search_id=result.search_id,
        zip_code=result.zip_code,
        candidates=[
            FacilityCandidateResponse(
                candidate_id=item.candidate_id,
                name=item.candidate.name,
                phone_e164=item.candidate.phone_e164,
                source_url=item.candidate.source_url,
            )
            for item in result.candidates
        ],
    )
