"""In-memory coordinator for one local-demo outbound call at a time."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .criteria import CriteriaRepository
from .livekit import (
    AgentDispatcher,
    DispatchRequest,
    IndeterminateDispatchError,
    RoomCleaner,
)
from .models import (
    TERMINAL_STATUSES,
    AgentEvent,
    CallOutcome,
    CallRecipient,
    CallRequest,
    CallStatus,
    CallStatusResponse,
    CallSummary,
    CriteriaEvidence,
    InternalContextResponse,
    PatientBrief,
    mask_phone_number,
)
from .patient_source import PatientSource

logger = logging.getLogger(__name__)


class CallNotFoundError(LookupError):
    pass


class ActiveCallError(RuntimeError):
    pass


class InvalidCapabilityError(PermissionError):
    pass


class ContextNotReadyError(RuntimeError):
    pass


class InvalidStateTransitionError(RuntimeError):
    pass


@dataclass(slots=True)
class CallRecord:
    call_id: str
    request: CallRequest | None
    destination_masked: str
    context_token: str | None
    room_name: str
    status: CallStatus
    created_at: datetime
    updated_at: datetime
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None
    outcome: CallOutcome | None = None
    summary: CallSummary | None = None
    patient: PatientBrief | None = None
    criteria_evidence: list[CriteriaEvidence] | None = None
    recipient: CallRecipient | None = None
    dispatch_in_progress: bool = False
    room_dispatched: bool = False
    room_cleanup_pending: bool = False


_ALLOWED_TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.PREPARING: frozenset({CallStatus.DISPATCHED, CallStatus.FAILED}),
    CallStatus.DISPATCHED: frozenset({CallStatus.DIALING, CallStatus.FAILED}),
    CallStatus.DIALING: frozenset(
        {CallStatus.SCREENING, CallStatus.ACTIVE, CallStatus.FAILED}
    ),
    CallStatus.SCREENING: frozenset(
        {
            CallStatus.ACTIVE,
            CallStatus.SUMMARIZING,
            CallStatus.COMPLETED,
            CallStatus.FAILED,
        }
    ),
    CallStatus.ACTIVE: frozenset(
        {CallStatus.SUMMARIZING, CallStatus.COMPLETED, CallStatus.FAILED}
    ),
    CallStatus.SUMMARIZING: frozenset({CallStatus.COMPLETED, CallStatus.FAILED}),
    CallStatus.COMPLETED: frozenset(),
    CallStatus.FAILED: frozenset(),
}


class CallCoordinator:
    """Owns volatile call context and protects the one-active-call invariant."""

    def __init__(
        self,
        *,
        patient_source: PatientSource,
        criteria_repository: CriteriaRepository,
        dispatcher: AgentDispatcher,
        timeout_seconds: float = 300,
        task_spawner: Callable[
            [Coroutine[Any, Any, Any]], object
        ] = asyncio.create_task,
        max_records: int = 50,
    ) -> None:
        self._patient_source = patient_source
        self._criteria_repository = criteria_repository
        self._dispatcher = dispatcher
        self._timeout_seconds = timeout_seconds
        self._task_spawner = task_spawner
        self._max_records = max_records
        self._records: dict[str, CallRecord] = {}
        self._lock = asyncio.Lock()

    async def create_call(
        self,
        request: CallRequest,
        *,
        recipient: CallRecipient | None = None,
    ) -> CallRecord:
        """Create a record and schedule preparation without awaiting I/O."""

        async with self._lock:
            if any(
                record.status not in TERMINAL_STATUSES
                or record.dispatch_in_progress
                or record.room_cleanup_pending
                for record in self._records.values()
            ):
                raise ActiveCallError("an outbound call is already active")
            self._prune_terminal_records_locked()
            now = _utcnow()
            call_id = str(uuid.uuid4())
            record = CallRecord(
                call_id=call_id,
                request=request,
                destination_masked=mask_phone_number(request.to_phone_number),
                context_token=secrets.token_urlsafe(32),
                room_name=f"coverage-{uuid.uuid4()}",
                status=CallStatus.PREPARING,
                created_at=now,
                updated_at=now,
                recipient=recipient or CallRecipient(),
            )
            self._records[call_id] = record

        # Scheduling occurs outside the lock.  The injected spawner gives unit
        # tests deterministic control and production uses asyncio.create_task.
        self._task_spawner(self.prepare_call(call_id))
        self._task_spawner(self._watch_timeout(call_id))
        logger.info("outbound_call_created call_id=%s", call_id)
        return record

    async def prepare_call(self, call_id: str) -> None:
        """Fetch safe context, look up evidence, then dispatch the worker."""

        record = await self._get_record(call_id)
        if record.status != CallStatus.PREPARING:
            return
        # Preserve just enough working data across awaits.  A concurrent
        # timeout clears the record's private context, but must not make an
        # in-flight preparation task dereference it afterwards.
        call_request = record.request
        context_token = record.context_token
        room_name = record.room_name
        if call_request is None or context_token is None:
            return

        try:
            patient = await self._patient_source.fetch_patient(call_request)
        except Exception:
            # Treat malformed or unavailable fixture context exactly like a
            # failed lookup: do not dial. Keep logs identifier-only.
            logger.warning("outbound_call_context_failed call_id=%s", call_id)
            await self._fail(call_id, CallOutcome.CONTEXT_ERROR)
            return

        try:
            evidence = await self._criteria_repository.find_criteria(
                payer=call_request.payer.value,
                plan_type=call_request.plan_type.value,
                drug=call_request.drug,
            )
        except Exception:
            # Missing/invalid future SQLite data is intentionally nonfatal.
            # The worker will use the restricted public Tavily fallback.
            logger.warning("outbound_call_criteria_unavailable call_id=%s", call_id)
            evidence = []

        if not await self._attach_context_if_active(call_id, patient, evidence):
            return

        # Set the observable lifecycle state before the cloud request.  A
        # dispatched worker can call back extremely quickly, and its dialing
        # event must not race against a still-"preparing" record.
        if not await self._mark_dispatch_started(call_id):
            return

        request = DispatchRequest(
            call_id=call_id,
            room_name=room_name,
            metadata=json.dumps(
                {"call_id": call_id, "token": context_token},
                separators=(",", ":"),
            ),
        )
        try:
            await self._dispatcher.dispatch(request)
        except IndeterminateDispatchError:
            logger.warning("outbound_call_dispatch_indeterminate call_id=%s", call_id)
            room_to_cleanup = await self._mark_dispatch_indeterminate(call_id)
            if room_to_cleanup is not None:
                await self._cleanup_possible_room(call_id, room_to_cleanup)
            return
        except Exception:
            # Keep call logs identifier-only; a provider exception could carry
            # request metadata or other sensitive transport details.
            logger.warning("outbound_call_dispatch_failed call_id=%s", call_id)
            await self._mark_dispatch_failed(call_id)
            await self._fail(
                call_id,
                CallOutcome.AGENT_ERROR,
                only_if_status=CallStatus.DISPATCHED,
            )
            return

        room_to_cleanup = await self._mark_dispatch_succeeded(call_id)
        if room_to_cleanup is not None:
            # Timeout can occur while the cloud dispatch request is in flight.
            # Clean up here rather than leave a just-created room/SIP leg alive.
            await self._cleanup_possible_room(call_id, room_to_cleanup)
            return
        logger.info("outbound_call_dispatched call_id=%s", call_id)

    async def public_status(self, call_id: str) -> CallStatusResponse:
        record = await self._get_record(call_id)
        return CallStatusResponse(
            call_id=record.call_id,
            status=record.status,
            destination_masked=record.destination_masked,
            created_at=record.created_at,
            updated_at=record.updated_at,
            dispatched_at=record.dispatched_at,
            completed_at=record.completed_at,
            outcome=record.outcome,
            summary=record.summary,
        )

    async def internal_context(
        self,
        call_id: str,
        token: str | None,
    ) -> InternalContextResponse:
        async with self._lock:
            record = self._require_authorized_locked(call_id, token)
            if record.patient is None or record.criteria_evidence is None:
                raise ContextNotReadyError("call context is not ready")
            if record.request is None or record.recipient is None:
                raise ContextNotReadyError("call context is not ready")
            return InternalContextResponse(
                call_id=record.call_id,
                to_phone_number=record.request.to_phone_number,
                payer=record.request.payer,
                plan_type=record.request.plan_type,
                drug=record.request.drug,
                recipient=record.recipient,
                patient={
                    "quickview_data": record.patient.quickview_data,
                    "banner_data": record.patient.banner_data,
                },
                criteria=[_evidence_payload(item) for item in record.criteria_evidence],
            )

    async def apply_agent_event(
        self,
        call_id: str,
        token: str | None,
        event: AgentEvent | CallStatus,
    ) -> CallStatusResponse:
        # Accepting a bare status keeps the coordinator simple for callers that
        # only have a lifecycle transition; HTTP routes always send AgentEvent.
        if isinstance(event, CallStatus):
            event = AgentEvent(status=event)
        event = _normalize_agent_event(event)
        async with self._lock:
            record = self._require_authorized_locked(call_id, token)
            self._validate_agent_event(record, event)
            if event.status != record.status:
                record.status = event.status
                record.updated_at = _utcnow()
                if event.status == CallStatus.DISPATCHED:
                    record.dispatched_at = record.updated_at
                if event.status in TERMINAL_STATUSES:
                    record.completed_at = record.updated_at
            if event.status == CallStatus.SUMMARIZING and event.summary is not None:
                # Retain only a redacted, transcript-free handoff while the
                # worker plays its farewell and deletes the SIP room.
                record.summary = _redact_summary(event.summary, record.patient)
            if event.status in TERMINAL_STATUSES:
                record.outcome = event.outcome
                if event.summary is not None:
                    record.summary = _redact_summary(event.summary, record.patient)
                self._clear_private_context_locked(record)
            return _public_response(record)

    async def expire_call(self, call_id: str) -> None:
        """Mark a still-active call as timed out; never overwrite a result."""

        room_to_cleanup: str | None = None
        async with self._lock:
            record = self._records.get(call_id)
            if record is None or record.status in TERMINAL_STATUSES:
                return
            now = _utcnow()
            record.status = CallStatus.FAILED
            record.outcome = CallOutcome.TIMEOUT
            record.updated_at = now
            record.completed_at = now
            if record.room_dispatched or record.dispatch_in_progress:
                record.room_cleanup_pending = True
                # A dispatch may have reached LiveKit just before its relay
                # acknowledgement was interrupted. Deleting this deterministic
                # room name is safe whether the room exists yet or not.
                room_to_cleanup = record.room_name
                cleanup_is_final = not record.dispatch_in_progress
            else:
                cleanup_is_final = True
            self._clear_private_context_locked(record)
        logger.warning("outbound_call_timed_out call_id=%s", call_id)
        if room_to_cleanup is not None:
            await self._cleanup_possible_room(
                call_id,
                room_to_cleanup,
                final=cleanup_is_final,
            )

    async def _watch_timeout(self, call_id: str) -> None:
        try:
            await asyncio.sleep(self._timeout_seconds)
            await self.expire_call(call_id)
        except asyncio.CancelledError:
            raise

    async def _get_record(self, call_id: str) -> CallRecord:
        async with self._lock:
            record = self._records.get(call_id)
            if record is None:
                raise CallNotFoundError(call_id)
            return record

    async def _attach_context_if_active(
        self,
        call_id: str,
        patient: PatientBrief,
        evidence: list[CriteriaEvidence],
    ) -> bool:
        async with self._lock:
            record = self._records.get(call_id)
            if record is None or record.status in TERMINAL_STATUSES:
                return False
            record.patient = patient
            record.criteria_evidence = evidence
            return True

    async def _mark_dispatch_started(self, call_id: str) -> bool:
        """Make worker callbacks legal before awaiting cloud dispatch."""

        async with self._lock:
            record = self._records.get(call_id)
            if record is None or record.status in TERMINAL_STATUSES:
                return False
            if CallStatus.DISPATCHED not in _ALLOWED_TRANSITIONS[record.status]:
                raise InvalidStateTransitionError(
                    f"cannot transition {record.status.value} to dispatched"
                )
            now = _utcnow()
            record.status = CallStatus.DISPATCHED
            record.updated_at = now
            record.dispatched_at = now
            record.dispatch_in_progress = True
            return True

    async def _mark_dispatch_succeeded(self, call_id: str) -> str | None:
        """Record dispatch success, or return a room raced by a timeout."""

        async with self._lock:
            record = self._records.get(call_id)
            if record is None:
                return None
            record.dispatch_in_progress = False
            record.room_dispatched = True
            if (
                record.status in TERMINAL_STATUSES
                and record.outcome == CallOutcome.TIMEOUT
            ):
                # A speculative delete may have run while dispatch was still
                # unknown. Run one final delete after a confirmed dispatch so
                # it cannot create a room after that speculative attempt.
                record.room_cleanup_pending = True
                return record.room_name
            return None

    async def _mark_dispatch_failed(self, call_id: str) -> None:
        """Release a timeout's cleanup hold when no room was ever created."""

        async with self._lock:
            record = self._records.get(call_id)
            if record is None:
                return
            record.dispatch_in_progress = False
            if (
                not record.room_dispatched
                and record.status in TERMINAL_STATUSES
                and record.outcome == CallOutcome.TIMEOUT
            ):
                # The relay definitively reported no dispatch, so an earlier
                # speculative timeout cleanup cannot have missed a room.
                record.room_cleanup_pending = False

    async def _mark_dispatch_indeterminate(self, call_id: str) -> str | None:
        """Fail safely and return the deterministic room that must be deleted."""

        async with self._lock:
            record = self._records.get(call_id)
            if record is None:
                return None
            record.dispatch_in_progress = False
            # Treat this as a possibly-created room. This keeps the active-call
            # gate closed until LiveKit confirms deletion (or NotFound).
            record.room_dispatched = True
            record.room_cleanup_pending = True
            if record.status not in TERMINAL_STATUSES:
                now = _utcnow()
                record.status = CallStatus.FAILED
                record.outcome = CallOutcome.AGENT_ERROR
                record.updated_at = now
                record.completed_at = now
                self._clear_private_context_locked(record)
            return record.room_name

    async def _fail(
        self,
        call_id: str,
        outcome: CallOutcome,
        *,
        only_if_status: CallStatus | None = None,
    ) -> None:
        async with self._lock:
            record = self._records.get(call_id)
            if record is None or record.status in TERMINAL_STATUSES:
                return
            if only_if_status is not None and record.status != only_if_status:
                return
            now = _utcnow()
            record.status = CallStatus.FAILED
            record.outcome = outcome
            record.updated_at = now
            record.completed_at = now
            self._clear_private_context_locked(record)

    async def _cleanup_possible_room(
        self,
        call_id: str,
        room_name: str,
        *,
        final: bool = True,
    ) -> None:
        """Delete a room after timeout without exposing transport identifiers."""

        cleanup_confirmed = False
        try:
            if not isinstance(self._dispatcher, RoomCleaner):
                logger.warning(
                    "outbound_call_room_cleanup_unavailable call_id=%s", call_id
                )
                return
            try:
                await self._dispatcher.delete_room(room_name)
                cleanup_confirmed = True
            except Exception:
                # A cleanup failure never changes the authoritative timeout
                # state. Avoid provider exception text/tracebacks because they
                # can contain request metadata.
                logger.warning("outbound_call_room_cleanup_failed call_id=%s", call_id)
        finally:
            # A speculative deletion during an in-flight dispatch is not proof
            # that a room will not appear later. Likewise, a failed cleanup
            # must keep the one-call gate closed rather than risk a second SIP
            # leg while the first may still exist.
            if cleanup_confirmed and final:
                await self._finish_room_cleanup(call_id, room_name)

    async def _finish_room_cleanup(self, call_id: str, room_name: str) -> None:
        async with self._lock:
            record = self._records.get(call_id)
            if record is not None and record.room_name == room_name:
                record.room_cleanup_pending = False

    def _require_authorized_locked(
        self,
        call_id: str,
        token: str | None,
    ) -> CallRecord:
        record = self._records.get(call_id)
        if record is None:
            raise CallNotFoundError(call_id)
        if (
            not token
            or not record.context_token
            or not secrets.compare_digest(record.context_token, token)
        ):
            raise InvalidCapabilityError("invalid call capability")
        return record

    @staticmethod
    def _validate_agent_event(record: CallRecord, event: AgentEvent) -> None:
        if record.status in TERMINAL_STATUSES:
            # Repeating an identical terminal callback is harmless and lets the
            # worker retry a lost HTTP response without changing stored data.
            if (
                event.status == record.status
                and event.outcome == record.outcome
                and (event.summary is None or event.summary == record.summary)
            ):
                return
            raise InvalidStateTransitionError("call already reached a terminal state")

        if event.status not in _ALLOWED_TRANSITIONS[record.status]:
            raise InvalidStateTransitionError(
                f"cannot transition {record.status.value} to {event.status.value}"
            )
        if event.status == CallStatus.SUMMARIZING:
            if event.outcome is not None:
                raise InvalidStateTransitionError(
                    "a summarizing status must not include an outcome"
                )
        elif event.status in TERMINAL_STATUSES:
            if event.outcome is None:
                raise InvalidStateTransitionError(
                    "a terminal status requires an outcome"
                )
        elif event.outcome is not None or event.summary is not None:
            raise InvalidStateTransitionError(
                "outcome and summary are only allowed for a terminal status"
            )

    def _prune_terminal_records_locked(self) -> None:
        terminal_records = [
            record
            for record in self._records.values()
            if record.status in TERMINAL_STATUSES
        ]
        excess = len(self._records) - self._max_records + 1
        if excess <= 0:
            return
        for record in sorted(terminal_records, key=lambda item: item.updated_at)[
            :excess
        ]:
            self._records.pop(record.call_id, None)

    @staticmethod
    def _clear_private_context_locked(record: CallRecord) -> None:
        """Keep only public status/summary after a call reaches terminal state."""

        record.patient = None
        record.criteria_evidence = None
        record.recipient = None
        record.context_token = None
        record.request = None


def _evidence_payload(evidence: CriteriaEvidence) -> dict[str, Any]:
    return {
        "text": evidence.text,
        "source_label": evidence.source_label,
        "source_url": evidence.source_url,
        "effective_date": (
            evidence.effective_date.isoformat() if evidence.effective_date else None
        ),
    }


def _public_response(record: CallRecord) -> CallStatusResponse:
    return CallStatusResponse(
        call_id=record.call_id,
        status=record.status,
        destination_masked=record.destination_masked,
        created_at=record.created_at,
        updated_at=record.updated_at,
        dispatched_at=record.dispatched_at,
        completed_at=record.completed_at,
        outcome=record.outcome,
        summary=record.summary,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _redact_summary(
    summary: CallSummary | None,
    patient: PatientBrief | None,
) -> CallSummary | None:
    """Best-effort guard against an agent echoing case context into GET data.

    The call prompt still forbids that disclosure, but a server-side literal
    pass provides a second boundary before the short summary outlives the
    per-call patient context.  It intentionally performs no logging.
    """

    if summary is None or patient is None:
        return summary
    literals = _patient_literals(patient)
    if not literals:
        return summary

    def redact(text: str | None) -> str | None:
        if text is None:
            return None
        for literal in literals:
            text = re.sub(re.escape(literal), "[redacted]", text, flags=re.IGNORECASE)
        return text

    sources = [
        source.model_copy(
            update={
                "label": redact(source.label),
                "url": redact(source.url),
            }
        )
        for source in summary.public_sources
    ]
    return summary.model_copy(
        update={
            "criteria_summary": [
                redact(line) or "[redacted]" for line in summary.criteria_summary
            ],
            "unresolved_questions": [
                redact(line) or "[redacted]" for line in summary.unresolved_questions
            ],
            "public_sources": sources,
        }
    )


def _patient_literals(patient: PatientBrief) -> tuple[str, ...]:
    """Collect nontrivial JSON-like patient leaves, longest first."""

    values: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, (list, tuple, set)):
            for nested in value:
                visit(nested)
        elif isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            # Do not turn ordinary one-character tokens (or an age's leading
            # digit) into blanket redactions.  Long values cover patient IDs,
            # names, free-text fields, and meaningful scalar identifiers.
            if len(text) >= 3:
                values.add(text)

    visit(patient.quickview_data)
    visit(patient.banner_data)
    return tuple(sorted(values, key=len, reverse=True))


def _normalize_agent_event(event: AgentEvent) -> AgentEvent:
    """Derive a safe terminal outcome from common SIP failure diagnostics.

    The worker can report an opaque provider error without turning that error
    into public status data.  Unknown errors still require an explicit outcome
    instead of being guessed.
    """

    if (
        event.status != CallStatus.FAILED
        or event.outcome is not None
        or not event.error
    ):
        return event
    outcome = _sip_outcome_from_error(event.error)
    if outcome is None:
        return event
    return event.model_copy(update={"outcome": outcome})


def _sip_outcome_from_error(error: str) -> CallOutcome | None:
    normalized = error.casefold().replace("_", "-")
    if any(
        marker in normalized
        for marker in (
            "no answer",
            "no-answer",
            "noanswer",
            "sip 408",
            "status 408",
        )
    ):
        return CallOutcome.NO_ANSWER
    if any(
        marker in normalized
        for marker in (
            "busy",
            "486",
            "temporarily unavailable",
            "sip 480",
            "status 480",
        )
    ):
        return CallOutcome.UNAVAILABLE
    if any(
        marker in normalized
        for marker in (
            "carrier error",
            "trunk failure",
            "sip 5",
            "status 5",
        )
    ):
        return CallOutcome.CARRIER_ERROR
    return None
