"""A pull-based LiveKit dispatch bridge for a Daytona-hosted backend.

Daytona owns the call record and private context.  A relay running on the
developer's Mac polls this broker, executes the LiveKit API operation using
local credentials, and reports a definitive result.  The backend never gives
the relay patient data or a destination: a dispatch operation contains only a
room name and the existing per-call capability metadata.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from .livekit import (
    DispatchError,
    DispatchRequest,
    IndeterminateDispatchError,
    validate_dispatch_metadata,
)

RelayOperationKind = Literal["create_dispatch", "delete_room"]
MIN_SHARED_SECRET_LENGTH = 24


def validate_shared_secret(value: str | None, *, name: str) -> str:
    """Require a nontrivial bearer secret without including it in errors."""

    normalized = (value or "").strip()
    if len(normalized) < MIN_SHARED_SECRET_LENGTH:
        raise ValueError(
            f"{name} must be set to a random value of at least "
            f"{MIN_SHARED_SECRET_LENGTH} characters"
        )
    return normalized


class RelayOperationNotFoundError(LookupError):
    """The relay reported a stale or already-completed operation."""


class RelayLeaseError(PermissionError):
    """The relay did not present the one-time lease for an operation."""


@dataclass(frozen=True, slots=True)
class RelayOperation:
    """The minimal work item a local relay is allowed to receive."""

    operation_id: str
    lease_token: str
    kind: RelayOperationKind
    room_name: str
    call_id: str | None = None
    agent_name: str | None = None
    metadata: str | None = None

    def to_payload(self) -> dict[str, str]:
        payload = {
            "operation_id": self.operation_id,
            "lease_token": self.lease_token,
            "kind": self.kind,
            "room_name": self.room_name,
        }
        if self.call_id is not None:
            payload["call_id"] = self.call_id
        if self.agent_name is not None:
            payload["agent_name"] = self.agent_name
        if self.metadata is not None:
            payload["metadata"] = self.metadata
        return payload


@dataclass(slots=True)
class _QueuedOperation:
    operation_id: str
    kind: RelayOperationKind
    room_name: str
    completion: asyncio.Future[None]
    call_id: str | None = None
    agent_name: str | None = None
    metadata: str | None = None
    lease_token: str | None = None


@dataclass(frozen=True, slots=True)
class _CompletedOperation:
    """Retain acknowledgements long enough for an interrupted relay retry."""

    lease_token: str
    succeeded: bool
    expires_at: float


class RelayBroker:
    """Queue and await one definitive result from the local relay.

    A leased operation is intentionally not re-delivered automatically.  A
    duplicate LiveKit dispatch could create a second SIP leg; if the relay
    disappears after taking work, the existing coordinator watchdog is the
    safe fail-closed outcome.
    """

    def __init__(
        self,
        *,
        operation_timeout_seconds: float = 45.0,
        heartbeat_ttl_seconds: float = 15.0,
        acknowledgement_ttl_seconds: float = 300.0,
    ) -> None:
        if (
            operation_timeout_seconds <= 0
            or heartbeat_ttl_seconds <= 0
            or acknowledgement_ttl_seconds <= 0
        ):
            raise ValueError("relay timeouts must be positive")
        self._operation_timeout_seconds = operation_timeout_seconds
        self._heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self._acknowledgement_ttl_seconds = acknowledgement_ttl_seconds
        self._operations: dict[str, _QueuedOperation] = {}
        self._completed: dict[str, _CompletedOperation] = {}
        self._last_poll_at: float | None = None
        self._lock = asyncio.Lock()

    async def queue_dispatch(self, request: DispatchRequest, *, agent_name: str) -> None:
        validate_dispatch_metadata(request.metadata)
        await self._queue_and_wait(
            kind="create_dispatch",
            room_name=request.room_name,
            call_id=request.call_id,
            agent_name=agent_name,
            metadata=request.metadata,
        )

    async def queue_room_delete(self, room_name: str) -> None:
        if not room_name.strip():
            raise DispatchError("relay room cleanup request is invalid")
        await self._queue_and_wait(kind="delete_room", room_name=room_name)

    async def poll(self) -> RelayOperation | None:
        """Lease one pending operation to the authenticated local relay."""

        async with self._lock:
            self._prune_completed_locked()
            self._last_poll_at = monotonic()
            for pending in self._operations.values():
                if pending.lease_token is not None:
                    continue
                pending.lease_token = secrets.token_urlsafe(24)
                return RelayOperation(
                    operation_id=pending.operation_id,
                    lease_token=pending.lease_token,
                    kind=pending.kind,
                    room_name=pending.room_name,
                    call_id=pending.call_id,
                    agent_name=pending.agent_name,
                    metadata=pending.metadata,
                )
        return None

    async def complete(
        self,
        *,
        operation_id: str,
        lease_token: str,
        succeeded: bool,
    ) -> None:
        """Resolve the backend operation only after local LiveKit execution."""

        async with self._lock:
            self._prune_completed_locked()
            pending = self._operations.get(operation_id)
            if pending is None:
                completed = self._completed.get(operation_id)
                if (
                    completed is not None
                    and completed.succeeded is succeeded
                    and secrets.compare_digest(completed.lease_token, lease_token)
                ):
                    # The backend completed the first acknowledgement but the
                    # relay lost its HTTP response. Treat the retry as a safe
                    # no-op so it never re-executes the LiveKit operation.
                    return
                raise RelayOperationNotFoundError("relay operation is unavailable")
            if (
                pending.lease_token is None
                or not secrets.compare_digest(pending.lease_token, lease_token)
            ):
                raise RelayLeaseError("relay operation lease is invalid")
            del self._operations[operation_id]
            self._completed[operation_id] = _CompletedOperation(
                lease_token=pending.lease_token,
                succeeded=succeeded,
                expires_at=monotonic() + self._acknowledgement_ttl_seconds,
            )

        if pending.completion.done():
            return
        if succeeded:
            pending.completion.set_result(None)
        elif pending.kind == "create_dispatch":
            # A local API error does not prove the remote dispatch failed: the
            # request may have been accepted before its response was lost.
            pending.completion.set_exception(
                IndeterminateDispatchError(
                    "local LiveKit relay could not confirm the dispatch operation"
                )
            )
        else:
            pending.completion.set_exception(
                DispatchError("local LiveKit relay could not complete the operation")
            )

    def health(self) -> dict[str, object]:
        last_poll_at = self._last_poll_at
        ready = (
            last_poll_at is not None
            and monotonic() - last_poll_at <= self._heartbeat_ttl_seconds
        )
        return {"ready": ready, "mode": "daytona-relay"}

    def is_ready(self) -> bool:
        return bool(self.health()["ready"])

    async def _queue_and_wait(
        self,
        *,
        kind: RelayOperationKind,
        room_name: str,
        call_id: str | None = None,
        agent_name: str | None = None,
        metadata: str | None = None,
    ) -> None:
        operation_id = secrets.token_urlsafe(18)
        completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        pending = _QueuedOperation(
            operation_id=operation_id,
            kind=kind,
            room_name=room_name,
            completion=completion,
            call_id=call_id,
            agent_name=agent_name,
            metadata=metadata,
        )
        async with self._lock:
            self._operations[operation_id] = pending

        try:
            await asyncio.wait_for(
                asyncio.shield(completion), timeout=self._operation_timeout_seconds
            )
        except TimeoutError as exc:
            async with self._lock:
                leased = pending.lease_token is not None
                still_pending = self._operations.get(operation_id) is pending
                if not leased and kind == "create_dispatch" and still_pending:
                    del self._operations[operation_id]
            if completion.done():
                await asyncio.shield(completion)
                return
            if not leased and kind == "create_dispatch":
                if not completion.done():
                    completion.cancel()
                raise DispatchError(
                    "local LiveKit relay did not receive the dispatch operation"
                ) from exc
            # A relay that has leased an operation may have completed the
            # LiveKit request just before its result acknowledgement was lost.
            # Do not turn that ambiguity into a new eligible call. A queued
            # room deletion is likewise safe to retain until it is confirmed.
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            async with self._lock:
                if self._operations.get(operation_id) is pending:
                    del self._operations[operation_id]
            if not completion.done():
                completion.cancel()
            raise

    def _prune_completed_locked(self) -> None:
        now = monotonic()
        for operation_id, completed in tuple(self._completed.items()):
            if completed.expires_at <= now:
                del self._completed[operation_id]


class RelayDispatcher:
    """Use the coordinator's existing dispatch and cleanup protocols remotely."""

    def __init__(self, broker: RelayBroker, *, agent_name: str = "voice") -> None:
        self._broker = broker
        self._agent_name = agent_name

    async def dispatch(self, request: DispatchRequest) -> None:
        await self._broker.queue_dispatch(request, agent_name=self._agent_name)

    async def delete_room(self, room_name: str) -> None:
        await self._broker.queue_room_delete(room_name)
