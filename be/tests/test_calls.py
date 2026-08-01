"""Contract tests for the isolated outbound-calling backend.

These tests deliberately inject fakes: they must not dial, read any external
patient system, or need LiveKit credentials.
"""

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from calling.coordinator import (
    ActiveCallError,
    CallCoordinator,
    InvalidStateTransitionError,
)
from calling.criteria import UnavailableCriteriaRepository
from calling.livekit import IndeterminateDispatchError
from calling.models import (
    AgentEvent,
    CallOutcome,
    CallRequest,
    CallStatus,
    CallSummary,
    PatientBrief,
    PublicSourceReference,
)
from calling.router import CallingServices, install_calling_router
from docsearch.serve import app


@dataclass
class FakePatientSource:
    brief: PatientBrief = field(default_factory=lambda: PatientBrief(
        quickview_data={"diagnosis": "synthetic diagnosis"},
        banner_data={"name": "Synthetic Patient"},
    ))

    async def fetch_patient(self, request: CallRequest) -> PatientBrief:
        assert request.patient_id == "sandbox-patient-id"
        return self.brief


@dataclass
class FakeDispatcher:
    requests: list[object] = field(default_factory=list)
    deleted_rooms: list[str] = field(default_factory=list)

    async def dispatch(self, request: object) -> None:
        self.requests.append(request)

    async def delete_room(self, room_name: str) -> None:
        self.deleted_rooms.append(room_name)


class DeferredSpawner:
    """Keeps endpoint tests deterministic without leaking pending coroutines."""

    def __init__(self) -> None:
        self.coroutines: list[object] = []

    def __call__(self, coroutine: object) -> None:
        coroutine.close()

    def close(self) -> None:
        self.coroutines.clear()


def _services(*, run_background: bool = False) -> tuple[CallingServices, FakeDispatcher, DeferredSpawner | None]:
    dispatcher = FakeDispatcher()
    deferred = None if run_background else DeferredSpawner()
    coordinator = CallCoordinator(
        patient_source=FakePatientSource(),
        criteria_repository=UnavailableCriteriaRepository(),
        dispatcher=dispatcher,
        timeout_seconds=300,
        task_spawner=asyncio.create_task if run_background else deferred,
    )
    return (
        CallingServices(coordinator=coordinator,
                        criteria_repository=UnavailableCriteriaRepository()),
        dispatcher,
        deferred,
    )


def _install(services: CallingServices) -> None:
    # The production entrypoint includes the router once.  Tests replace only
    # its dependencies, never the global FastAPI object or docsearch routes.
    install_calling_router(app, services=services)


def _request_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "to_phone_number": "+12125550123",
        "patient_id": "sandbox-patient-id",
        "payer": "aetna",
        "plan_type": "commercial",
        "drug": "pembrolizumab",
    }
    body.update(overrides)
    return body


def test_post_validates_us_e164_and_payer_plan_enums() -> None:
    services, _, deferred = _services()
    _install(services)
    try:
        with TestClient(app) as client:
            invalid_phone = client.post("/calls", json=_request_body(
                to_phone_number="+442071838750"))
            invalid_payer = client.post("/calls", json=_request_body(
                payer="unknown"))
            invalid_plan = client.post("/calls", json=_request_body(
                plan_type="exchange"))

        assert invalid_phone.status_code == 422
        assert invalid_payer.status_code == 422
        assert invalid_plan.status_code == 422
    finally:
        assert deferred is not None
        deferred.close()


def test_post_returns_202_and_blocks_a_second_active_call() -> None:
    services, _, deferred = _services()
    _install(services)
    try:
        with TestClient(app) as client:
            first = client.post("/calls", json=_request_body())
            second = client.post("/calls", json=_request_body(
                patient_id="another-sandbox-patient"))

            assert first.status_code == 202
            assert first.json()["status"] == "preparing"
            assert first.json()["status_url"].endswith(
                f"/calls/{first.json()['call_id']}")
            assert second.status_code == 409

            status = client.get(first.json()["status_url"])
            assert status.status_code == 200
            assert status.json()["destination_masked"].endswith("0123")
            assert "+12125550123" not in status.text
            assert "token" not in status.text
            assert "patient" not in status.json()
    finally:
        assert deferred is not None
        deferred.close()


def test_internal_context_and_events_require_the_per_call_capability() -> None:
    services, dispatcher, _ = _services()
    _install(services)
    request = CallRequest(**_request_body())
    record = asyncio.run(services.coordinator.create_call(request))
    asyncio.run(services.coordinator.prepare_call(record.call_id))

    assert len(dispatcher.requests) == 1
    dispatch = dispatcher.requests[0]
    token = json.loads(dispatch.metadata)["token"]

    with TestClient(app) as client:
        missing = client.get(f"/internal/calls/{record.call_id}/context")
        wrong = client.get(
            f"/internal/calls/{record.call_id}/context",
            headers={"X-Call-Context-Token": "not-the-token"},
        )
        correct = client.get(
            f"/internal/calls/{record.call_id}/context",
            headers={"X-Call-Context-Token": token},
        )
        denied_event = client.post(
            f"/internal/calls/{record.call_id}/events",
            headers={"X-Call-Context-Token": "not-the-token"},
            json={"status": "dialing"},
        )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert denied_event.status_code == 403
    assert correct.status_code == 200
    assert correct.json()["to_phone_number"] == "+12125550123"
    assert correct.json()["patient"]["quickview_data"] == {
        "diagnosis": "synthetic diagnosis"}


def test_dispatch_metadata_is_sanitized_and_agent_summary_is_public_safe() -> None:
    services, dispatcher, _ = _services()
    _install(services)
    record = asyncio.run(services.coordinator.create_call(CallRequest(**_request_body())))
    asyncio.run(services.coordinator.prepare_call(record.call_id))

    metadata = json.loads(dispatcher.requests[0].metadata)
    assert set(metadata) == {"call_id", "token"}
    metadata_text = json.dumps(metadata)
    assert "+12125550123" not in metadata_text
    assert "sandbox-patient-id" not in metadata_text

    token = metadata["token"]
    with TestClient(app) as client:
        assert client.post(
            f"/internal/calls/{record.call_id}/events",
            headers={"X-Call-Context-Token": token},
            json={"status": "dialing"},
        ).status_code == 200
        assert client.post(
            f"/internal/calls/{record.call_id}/events",
            headers={"X-Call-Context-Token": token},
            json={"status": "screening"},
        ).status_code == 200
        assert client.post(
            f"/internal/calls/{record.call_id}/events",
            headers={"X-Call-Context-Token": token},
            json={"status": "active"},
        ).status_code == 200
        completed = client.post(
            f"/internal/calls/{record.call_id}/events",
            headers={"X-Call-Context-Token": token},
            json={
                "status": "completed",
                "outcome": "completed",
                "summary": {
                    "criteria_summary": ["Prior authorization is required."],
                    "unresolved_questions": ["Confirm treatment line."],
                    "public_sources": [{
                        "label": "Aetna policy",
                        "url": "https://www.aetna.com/health-care-professionals/clinical-policy-bulletins.html",
                    }],
                },
            },
        )
        public = client.get(f"/calls/{record.call_id}")
        terminal_context = client.get(
            f"/internal/calls/{record.call_id}/context",
            headers={"X-Call-Context-Token": token},
        )

    assert completed.status_code == 200
    assert public.json()["status"] == "completed"
    assert public.json()["outcome"] == "completed"
    assert public.json()["summary"]["criteria_summary"] == [
        "Prior authorization is required."]
    assert "synthetic diagnosis" not in public.text
    assert "Synthetic Patient" not in public.text
    assert terminal_context.status_code == 403
    assert record.patient is None
    assert record.criteria_evidence is None
    assert record.context_token is None
    assert record.request is None


def test_timeout_marks_an_active_call_with_a_precise_terminal_outcome() -> None:
    services, dispatcher, _ = _services()
    record = asyncio.run(services.coordinator.create_call(CallRequest(**_request_body())))
    asyncio.run(services.coordinator.prepare_call(record.call_id))
    token = json.loads(dispatcher.requests[0].metadata)["token"]
    asyncio.run(services.coordinator.apply_agent_event(
        record.call_id, token, CallStatus.DIALING))
    asyncio.run(services.coordinator.expire_call(record.call_id))

    status = asyncio.run(services.coordinator.public_status(record.call_id))
    assert status.status == CallStatus.FAILED
    assert status.outcome == "timeout"
    assert dispatcher.deleted_rooms == [record.room_name]


def test_timeout_keeps_the_one_call_gate_closed_when_room_cleanup_fails() -> None:
    class FailingCleanupDispatcher:
        async def dispatch(self, _: object) -> None:
            return None

        async def delete_room(self, _: str) -> None:
            raise RuntimeError("cleanup transport is unavailable")

    async def scenario() -> None:
        deferred = DeferredSpawner()
        coordinator = CallCoordinator(
            patient_source=FakePatientSource(),
            criteria_repository=UnavailableCriteriaRepository(),
            dispatcher=FailingCleanupDispatcher(),
            task_spawner=deferred,
        )
        try:
            record = await coordinator.create_call(CallRequest(**_request_body()))
            await coordinator.prepare_call(record.call_id)
            await coordinator.expire_call(record.call_id)

            with pytest.raises(ActiveCallError):
                await coordinator.create_call(CallRequest(**_request_body()))
        finally:
            deferred.close()

    asyncio.run(scenario())


def test_indeterminate_dispatch_deletes_the_possible_room_before_reopening_gate() -> None:
    class AmbiguousDispatcher:
        def __init__(self) -> None:
            self.deleted_rooms: list[str] = []

        async def dispatch(self, _: object) -> None:
            # Model a network failure after LiveKit could already have accepted
            # the dispatch, so the coordinator must not treat it as a clean
            # no-op.
            raise IndeterminateDispatchError("LiveKit response was interrupted")

        async def delete_room(self, room_name: str) -> None:
            self.deleted_rooms.append(room_name)

    async def scenario() -> None:
        dispatcher = AmbiguousDispatcher()
        deferred = DeferredSpawner()
        coordinator = CallCoordinator(
            patient_source=FakePatientSource(),
            criteria_repository=UnavailableCriteriaRepository(),
            dispatcher=dispatcher,
            task_spawner=deferred,
        )
        try:
            record = await coordinator.create_call(CallRequest(**_request_body()))
            await coordinator.prepare_call(record.call_id)

            status = await coordinator.public_status(record.call_id)
            assert status.status == CallStatus.FAILED
            assert status.outcome == CallOutcome.AGENT_ERROR
            assert dispatcher.deleted_rooms == [record.room_name]

            next_record = await coordinator.create_call(CallRequest(**_request_body()))
            assert next_record.call_id != record.call_id
        finally:
            deferred.close()

    asyncio.run(scenario())


def test_invalid_agent_state_transition_is_rejected() -> None:
    services, dispatcher, deferred = _services()
    try:
        record = asyncio.run(services.coordinator.create_call(
            CallRequest(**_request_body())))
        asyncio.run(services.coordinator.prepare_call(record.call_id))
        token = json.loads(dispatcher.requests[0].metadata)["token"]

        with pytest.raises(InvalidStateTransitionError):
            asyncio.run(services.coordinator.apply_agent_event(
                record.call_id, token, CallStatus.ACTIVE))
    finally:
        assert deferred is not None
        deferred.close()


def test_patient_source_failure_prevents_dispatch_before_dialing() -> None:
    @dataclass
    class BrokenPatientSource:
        async def fetch_patient(self, request: CallRequest) -> PatientBrief:
            raise RuntimeError("malformed")

    dispatcher = FakeDispatcher()
    deferred = DeferredSpawner()
    coordinator = CallCoordinator(
        patient_source=BrokenPatientSource(),
        criteria_repository=UnavailableCriteriaRepository(),
        dispatcher=dispatcher,
        task_spawner=deferred,
    )
    try:
        record = asyncio.run(coordinator.create_call(CallRequest(**_request_body())))
        asyncio.run(coordinator.prepare_call(record.call_id))
        status = asyncio.run(coordinator.public_status(record.call_id))

        assert dispatcher.requests == []
        assert dispatcher.deleted_rooms == []
        assert status.status == CallStatus.FAILED
        assert status.outcome == "context-error"
    finally:
        deferred.close()


def test_fast_agent_callback_can_follow_dispatch_without_a_state_race() -> None:
    class CallbackDispatcher:
        def __init__(self) -> None:
            self.coordinator: CallCoordinator | None = None
            self.requests: list[object] = []
            self.deleted_rooms: list[str] = []

        async def dispatch(self, request: object) -> None:
            self.requests.append(request)
            assert self.coordinator is not None
            token = json.loads(request.metadata)["token"]
            await self.coordinator.apply_agent_event(
                request.call_id, token, CallStatus.DIALING)

        async def delete_room(self, room_name: str) -> None:
            self.deleted_rooms.append(room_name)

    dispatcher = CallbackDispatcher()
    deferred = DeferredSpawner()
    coordinator = CallCoordinator(
        patient_source=FakePatientSource(),
        criteria_repository=UnavailableCriteriaRepository(),
        dispatcher=dispatcher,
        task_spawner=deferred,
    )
    dispatcher.coordinator = coordinator
    try:
        record = asyncio.run(coordinator.create_call(CallRequest(**_request_body())))
        asyncio.run(coordinator.prepare_call(record.call_id))

        status = asyncio.run(coordinator.public_status(record.call_id))
        assert status.status == CallStatus.DIALING
        assert len(dispatcher.requests) == 1
    finally:
        deferred.close()


def test_terminal_summary_redacts_literal_context_values_before_retention() -> None:
    private_value = "TOP SECRET CONTEXT SENTINEL"
    dispatcher = FakeDispatcher()
    deferred = DeferredSpawner()
    coordinator = CallCoordinator(
        patient_source=FakePatientSource(brief=PatientBrief(
            quickview_data={"note": private_value},
            banner_data={"member": "synthetic-member"},
        )),
        criteria_repository=UnavailableCriteriaRepository(),
        dispatcher=dispatcher,
        task_spawner=deferred,
    )
    try:
        record = asyncio.run(coordinator.create_call(CallRequest(**_request_body())))
        asyncio.run(coordinator.prepare_call(record.call_id))
        token = json.loads(dispatcher.requests[0].metadata)["token"]
        for lifecycle in (CallStatus.DIALING, CallStatus.SCREENING, CallStatus.ACTIVE):
            asyncio.run(coordinator.apply_agent_event(record.call_id, token, lifecycle))
        asyncio.run(coordinator.apply_agent_event(
            record.call_id,
            token,
            AgentEvent(
                status=CallStatus.COMPLETED,
                outcome=CallOutcome.COMPLETED,
                summary=CallSummary(
                    criteria_summary=[f"Found {private_value} in the record."],
                    unresolved_questions=[private_value.lower()],
                    public_sources=[PublicSourceReference(
                        label=private_value,
                        url="https://example.test/policy",
                    )],
                ),
            ),
        ))

        public = asyncio.run(coordinator.public_status(record.call_id))
        serialized = public.model_dump_json().lower()
        assert private_value.lower() not in serialized
        assert "[redacted]" in serialized
    finally:
        deferred.close()


def test_summarizing_retains_redacted_summary_until_post_hangup_terminal_event() -> None:
    private_value = "CONTEXT TWO PHASE SENTINEL"
    dispatcher = FakeDispatcher()
    deferred = DeferredSpawner()
    coordinator = CallCoordinator(
        patient_source=FakePatientSource(brief=PatientBrief(
            quickview_data={"note": private_value},
            banner_data={},
        )),
        criteria_repository=UnavailableCriteriaRepository(),
        dispatcher=dispatcher,
        task_spawner=deferred,
    )
    try:
        record = asyncio.run(coordinator.create_call(CallRequest(**_request_body())))
        asyncio.run(coordinator.prepare_call(record.call_id))
        token = json.loads(dispatcher.requests[0].metadata)["token"]
        for lifecycle in (CallStatus.DIALING, CallStatus.SCREENING, CallStatus.ACTIVE):
            asyncio.run(coordinator.apply_agent_event(record.call_id, token, lifecycle))

        interim = asyncio.run(coordinator.apply_agent_event(
            record.call_id,
            token,
            AgentEvent(
                status=CallStatus.SUMMARIZING,
                summary=CallSummary(criteria_summary=[private_value]),
            ),
        ))

        assert interim.status == CallStatus.SUMMARIZING
        assert interim.outcome is None
        assert interim.summary is not None
        assert interim.summary.criteria_summary == ["[redacted]"]
        assert record.patient is not None
        assert record.context_token == token
        assert asyncio.run(coordinator.internal_context(record.call_id, token)).call_id == record.call_id
        with pytest.raises(ActiveCallError):
            asyncio.run(coordinator.create_call(CallRequest(**_request_body())))

        completed = asyncio.run(coordinator.apply_agent_event(
            record.call_id,
            token,
            AgentEvent(status=CallStatus.COMPLETED, outcome=CallOutcome.COMPLETED),
        ))

        assert completed.status == CallStatus.COMPLETED
        assert completed.summary is not None
        assert completed.summary.criteria_summary == ["[redacted]"]
        assert record.patient is None
        assert record.criteria_evidence is None
        assert record.context_token is None
        next_record = asyncio.run(coordinator.create_call(CallRequest(**_request_body())))
        assert next_record.call_id != record.call_id
    finally:
        deferred.close()


@pytest.mark.parametrize(("diagnostic", "expected_outcome"), [
    ("SIP 408 no answer", CallOutcome.NO_ANSWER),
    ("SIP 486 Busy Here", CallOutcome.UNAVAILABLE),
])
def test_sip_error_callback_maps_no_answer_and_busy_without_exposing_error(
    diagnostic: str,
    expected_outcome: CallOutcome,
) -> None:
    dispatcher = FakeDispatcher()
    deferred = DeferredSpawner()
    coordinator = CallCoordinator(
        patient_source=FakePatientSource(),
        criteria_repository=UnavailableCriteriaRepository(),
        dispatcher=dispatcher,
        task_spawner=deferred,
    )
    try:
        record = asyncio.run(coordinator.create_call(CallRequest(**_request_body())))
        asyncio.run(coordinator.prepare_call(record.call_id))
        token = json.loads(dispatcher.requests[0].metadata)["token"]
        asyncio.run(coordinator.apply_agent_event(
            record.call_id, token, CallStatus.DIALING))

        result = asyncio.run(coordinator.apply_agent_event(
            record.call_id,
            token,
            AgentEvent(status=CallStatus.FAILED, error=diagnostic),
        ))

        assert result.status == CallStatus.FAILED
        assert result.outcome == expected_outcome
        assert diagnostic not in result.model_dump_json()
        assert record.context_token is None
    finally:
        deferred.close()


def test_timeout_holds_the_one_call_gate_until_inflight_dispatch_is_cleaned() -> None:
    class BlockingDispatcher:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.deleted_rooms: list[str] = []

        async def dispatch(self, request: object) -> None:
            self.started.set()
            await self.release.wait()

        async def delete_room(self, room_name: str) -> None:
            self.deleted_rooms.append(room_name)

    async def scenario() -> None:
        dispatcher = BlockingDispatcher()
        deferred = DeferredSpawner()
        coordinator = CallCoordinator(
            patient_source=FakePatientSource(),
            criteria_repository=UnavailableCriteriaRepository(),
            dispatcher=dispatcher,
            task_spawner=deferred,
        )
        try:
            record = await coordinator.create_call(CallRequest(**_request_body()))
            preparing = asyncio.create_task(coordinator.prepare_call(record.call_id))
            await dispatcher.started.wait()

            await coordinator.expire_call(record.call_id)
            with pytest.raises(ActiveCallError):
                await coordinator.create_call(CallRequest(**_request_body()))

            dispatcher.release.set()
            await preparing
            # The first delete is speculative while dispatch is unresolved;
            # the second closes a room that appeared after that attempt.
            assert dispatcher.deleted_rooms == [record.room_name, record.room_name]

            next_record = await coordinator.create_call(CallRequest(**_request_body()))
            assert next_record.call_id != record.call_id
        finally:
            deferred.close()

    asyncio.run(scenario())


def test_health_reports_calling_readiness_when_legacy_index_is_absent(
    monkeypatch, tmp_path,
) -> None:
    services, _, deferred = _services()
    _install(services)
    try:
        import docsearch.serve as serve

        monkeypatch.setattr(serve, "DB_PATH", tmp_path / "does-not-exist.db")
        payload = serve.health()

        assert payload["docsearch"] == "unavailable"
        assert payload["calling"] == {
            "criteria_repository": {"ready": False, "mode": "unavailable"},
            "facility_discovery": {"ready": False, "mode": "unavailable"},
        }
    finally:
        assert deferred is not None
        deferred.close()
