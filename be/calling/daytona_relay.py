"""Loopback-only Mac relay for a Daytona-hosted outbound-call backend.

The Daytona backend retains call state and context, but this process performs
the two operations Daytona cannot currently perform on this project: connecting
to LiveKit Cloud and receiving the worker's localhost-only callback traffic.
It polls only the intentionally narrow relay API and exposes only the two
existing capability-protected worker routes on a local loopback address.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Response, status

from .livekit import DispatchRequest, validate_dispatch_metadata
from .relay import validate_shared_secret
from .models import AgentEvent

logger = logging.getLogger(__name__)

_RELAY_POLL_PATH = "/internal/relay/poll"
_RELAY_RESULT_PATH = "/internal/relay/operations/{operation_id}/result"
_WORKER_CONTEXT_PATH = "/internal/calls/{call_id}/context"
_WORKER_EVENT_PATH = "/internal/calls/{call_id}/events"
_SUPPORTED_AGENT_NAME = "voice"
_RELAY_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class LiveKitOperationExecutor(Protocol):
    """The two local LiveKit operations the relay is allowed to invoke."""

    async def dispatch(self, request: DispatchRequest) -> None:
        ...

    async def delete_room(self, room_name: str) -> None:
        ...


class DaytonaRelayConfigurationError(RuntimeError):
    """The local relay is missing a safe, complete configuration."""


class DaytonaRelayProtocolError(RuntimeError):
    """The remote backend returned an unexpected relay response."""


@dataclass(frozen=True, slots=True)
class RemoteRelayOperation:
    """One minimal operation received from the Daytona backend."""

    operation_id: str
    lease_token: str
    kind: str
    room_name: str
    call_id: str | None = None
    agent_name: str | None = None
    metadata: str | None = None

    @classmethod
    def from_payload(cls, payload: object) -> RemoteRelayOperation:
        if not isinstance(payload, dict):
            raise DaytonaRelayProtocolError("relay operation payload is invalid")
        operation_id = _required_string(payload, "operation_id")
        lease_token = _required_string(payload, "lease_token")
        kind = _required_string(payload, "kind")
        room_name = _required_string(payload, "room_name")
        _validate_relay_token(operation_id)
        _validate_relay_token(lease_token)
        _validate_coverage_room_name(room_name)
        if kind == "delete_room":
            _reject_unexpected_fields(
                payload,
                {"operation_id", "lease_token", "kind", "room_name"},
            )
            return cls(
                operation_id=operation_id,
                lease_token=lease_token,
                kind=kind,
                room_name=room_name,
            )
        if kind != "create_dispatch":
            raise DaytonaRelayProtocolError("relay operation kind is invalid")
        _reject_unexpected_fields(
            payload,
            {
                "operation_id",
                "lease_token",
                "kind",
                "room_name",
                "call_id",
                "agent_name",
                "metadata",
            },
        )
        call_id = _required_string(payload, "call_id")
        agent_name = _required_string(payload, "agent_name")
        metadata = _required_string(payload, "metadata")
        if agent_name != _SUPPORTED_AGENT_NAME:
            raise DaytonaRelayProtocolError("relay agent name is invalid")
        validate_dispatch_metadata(metadata)
        try:
            metadata_call_id = json.loads(metadata)["call_id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise DaytonaRelayProtocolError("relay operation metadata is invalid") from exc
        if metadata_call_id != call_id:
            raise DaytonaRelayProtocolError("relay operation call ID is invalid")
        _validate_uuid(call_id)
        return cls(
            operation_id=operation_id,
            lease_token=lease_token,
            kind=kind,
            room_name=room_name,
            call_id=call_id,
            agent_name=agent_name,
            metadata=metadata,
        )


def validate_daytona_backend_url(value: str) -> str:
    """Accept only a bare HTTPS Daytona Preview URL for the relay."""

    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DaytonaRelayConfigurationError(
            "DAYTONA_BACKEND_URL must be a bare HTTPS Preview URL"
        )
    return f"https://{parsed.netloc}"


def relay_configuration_from_environment(
    environment: Mapping[str, str | None],
) -> tuple[str, str, str, str, str]:
    """Read the local-only values without leaking them in error messages."""

    backend_url = validate_daytona_backend_url(
        (environment.get("DAYTONA_BACKEND_URL") or "").strip()
    )
    relay_secret = (environment.get("CALL_RELAY_SECRET") or "").strip()
    livekit_url = (environment.get("LIVEKIT_URL") or "").strip()
    api_key = (environment.get("LIVEKIT_API_KEY") or "").strip()
    api_secret = (environment.get("LIVEKIT_API_SECRET") or "").strip()
    try:
        relay_secret = validate_shared_secret(relay_secret, name="CALL_RELAY_SECRET")
    except ValueError as exc:
        raise DaytonaRelayConfigurationError(str(exc)) from exc
    if not all((livekit_url, api_key, api_secret)):
        raise DaytonaRelayConfigurationError(
            "LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are required"
        )
    return backend_url, relay_secret, livekit_url, api_key, api_secret


class DaytonaRelay:
    """Poll the remote broker and proxy the worker's two fixed callbacks."""

    def __init__(
        self,
        *,
        backend_url: str,
        relay_secret: str,
        livekit_dispatcher: LiveKitOperationExecutor,
        poll_interval_seconds: float = 0.75,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        self._backend_url = validate_daytona_backend_url(backend_url)
        try:
            relay_secret = validate_shared_secret(
                relay_secret, name="CALL_RELAY_SECRET"
            )
        except ValueError as exc:
            raise DaytonaRelayConfigurationError(str(exc)) from exc
        self._authorization = f"Bearer {relay_secret}"
        self._livekit_dispatcher = livekit_dispatcher
        self._poll_interval_seconds = poll_interval_seconds
        self._client = client
        self._owns_client = client is None
        self._pending_result: tuple[RemoteRelayOperation, bool] | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._backend_url,
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
            )

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def poll_once(self) -> bool:
        """Do at most one remote operation and return whether work was found."""

        client = self._require_client()
        if self._pending_result is not None:
            await self._report_pending_result(client)
            return True
        response = await client.post(
            _RELAY_POLL_PATH,
            headers={"Authorization": self._authorization},
        )
        if response.status_code == status.HTTP_204_NO_CONTENT:
            return False
        if response.status_code != status.HTTP_200_OK:
            raise DaytonaRelayProtocolError("relay poll was rejected")
        operation = RemoteRelayOperation.from_payload(response.json())
        succeeded = False
        try:
            await self._execute(operation)
        except Exception:
            # Provider exception text can contain request internals. Keep the
            # Mac log independently useful without logging tokens or metadata.
            logger.warning(
                "daytona_relay_operation_failed operation_id=%s kind=%s",
                operation.operation_id,
                operation.kind,
            )
        else:
            succeeded = True
        # If this acknowledgement fails, retain it in memory and retry only
        # that acknowledgement. Re-polling and re-executing the dispatch could
        # create a duplicate worker or SIP leg.
        self._pending_result = (operation, succeeded)
        await self._report_pending_result(client)
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Maintain the backend heartbeat while the local relay is running."""

        while not stop_event.is_set():
            try:
                processed = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "daytona_relay_poll_failed error_type=%s", type(exc).__name__
                )
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self._poll_interval_seconds
                    )
                except TimeoutError:
                    pass

    async def get_worker_context(
        self, *, call_id: str, context_token: str | None
    ) -> httpx.Response:
        """Forward one worker context request, preserving only its capability."""

        return await self._require_client().get(
            _WORKER_CONTEXT_PATH.format(call_id=call_id),
            headers=_context_headers(context_token),
        )

    async def post_worker_event(
        self,
        *,
        call_id: str,
        context_token: str | None,
        event: AgentEvent,
    ) -> httpx.Response:
        """Forward one structured worker event, not an arbitrary HTTP request."""

        return await self._require_client().post(
            _WORKER_EVENT_PATH.format(call_id=call_id),
            headers=_context_headers(context_token),
            json=event.model_dump(mode="json", exclude_none=True),
        )

    async def _execute(self, operation: RemoteRelayOperation) -> None:
        if operation.kind == "create_dispatch":
            assert operation.call_id is not None
            assert operation.metadata is not None
            await self._livekit_dispatcher.dispatch(
                DispatchRequest(
                    call_id=operation.call_id,
                    room_name=operation.room_name,
                    metadata=operation.metadata,
                )
            )
            return
        if operation.kind == "delete_room":
            await self._livekit_dispatcher.delete_room(operation.room_name)
            return
        raise DaytonaRelayProtocolError("relay operation kind is invalid")

    async def _report_pending_result(self, client: httpx.AsyncClient) -> None:
        pending = self._pending_result
        if pending is None:
            return
        operation, succeeded = pending
        result = await client.post(
            _RELAY_RESULT_PATH.format(operation_id=quote(operation.operation_id, safe="")),
            headers={"Authorization": self._authorization},
            json={"lease_token": operation.lease_token, "succeeded": succeeded},
        )
        if result.status_code != status.HTTP_204_NO_CONTENT:
            raise DaytonaRelayProtocolError("relay result was rejected")
        self._pending_result = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Daytona relay client has not started")
        return self._client


def build_daytona_relay_app(relay: DaytonaRelay) -> FastAPI:
    """Create the small loopback service consumed by the local worker only."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await relay.start()
        stop_event = asyncio.Event()
        poller = asyncio.create_task(relay.run_forever(stop_event))
        try:
            yield
        finally:
            stop_event.set()
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass
            await relay.close()

    app = FastAPI(title="Daytona local LiveKit relay", lifespan=lifespan)

    @app.get("/internal/calls/{call_id}/context", include_in_schema=False)
    async def get_context(
        call_id: str,
        x_call_context_token: str | None = Header(
            default=None, alias="X-Call-Context-Token"
        ),
    ) -> Response:
        return await _proxy_response(
            relay.get_worker_context(
                call_id=call_id,
                context_token=x_call_context_token,
            )
        )

    @app.post("/internal/calls/{call_id}/events", include_in_schema=False)
    async def post_event(
        call_id: str,
        event: AgentEvent,
        x_call_context_token: str | None = Header(
            default=None, alias="X-Call-Context-Token"
        ),
    ) -> Response:
        return await _proxy_response(
            relay.post_worker_event(
                call_id=call_id,
                context_token=x_call_context_token,
                event=event,
            )
        )

    return app


async def _proxy_response(response_awaitable: Awaitable[httpx.Response]) -> Response:
    try:
        response = await response_awaitable
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the Daytona backend is unreachable",
        ) from exc
    content_type = response.headers.get("content-type")
    headers = {"content-type": content_type} if content_type else None
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=headers,
    )


def _required_string(payload: dict[object, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise DaytonaRelayProtocolError("relay operation payload is invalid")
    return value


def _validate_relay_token(value: str) -> None:
    if not _RELAY_TOKEN_PATTERN.fullmatch(value):
        raise DaytonaRelayProtocolError("relay operation payload is invalid")


def _validate_uuid(value: str) -> None:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise DaytonaRelayProtocolError("relay operation call ID is invalid") from exc
    if str(parsed) != value:
        raise DaytonaRelayProtocolError("relay operation call ID is invalid")


def _validate_coverage_room_name(value: str) -> None:
    prefix = "coverage-"
    if not value.startswith(prefix):
        raise DaytonaRelayProtocolError("relay operation room is invalid")
    _validate_uuid(value.removeprefix(prefix))


def _reject_unexpected_fields(
    payload: dict[object, object], allowed: set[str]
) -> None:
    if set(payload) != allowed:
        raise DaytonaRelayProtocolError("relay operation payload is invalid")


def _context_headers(context_token: str | None) -> dict[str, str]:
    if context_token is None:
        return {}
    return {"X-Call-Context-Token": context_token}
