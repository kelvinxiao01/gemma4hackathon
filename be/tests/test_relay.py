"""Offline contracts for the Daytona-to-Mac LiveKit dispatch relay."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from calling.coordinator import ActiveCallError, CallCoordinator
from calling.criteria import UnavailableCriteriaRepository
from calling.livekit import DispatchRequest, IndeterminateDispatchError
from calling.models import CallRequest, PatientBrief
from calling.relay import RelayBroker, RelayDispatcher
from calling.router import CallingServices, build_calling_services, install_calling_router


def _request() -> DispatchRequest:
    return DispatchRequest(
        call_id="call-123",
        room_name="coverage-room-123",
        metadata=json.dumps(
            {"call_id": "call-123", "token": "per-call-capability"},
            separators=(",", ":"),
        ),
    )


def test_relay_dispatch_waits_for_one_sanitized_local_execution() -> None:
    async def scenario() -> None:
        broker = RelayBroker(operation_timeout_seconds=1)
        dispatcher = RelayDispatcher(broker)

        dispatching = asyncio.create_task(dispatcher.dispatch(_request()))
        await asyncio.sleep(0)
        operation = await broker.poll()

        assert operation is not None
        assert operation.kind == "create_dispatch"
        assert operation.call_id == "call-123"
        assert operation.room_name == "coverage-room-123"
        assert json.loads(operation.metadata or "{}") == {
            "call_id": "call-123",
            "token": "per-call-capability",
        }
        serialized_operation = json.dumps(operation.to_payload())
        assert "+1" not in serialized_operation
        assert "patient" not in serialized_operation

        await broker.complete(
            operation_id=operation.operation_id,
            lease_token=operation.lease_token,
            succeeded=True,
        )
        await dispatching
        assert await broker.poll() is None

    asyncio.run(scenario())


def test_relay_http_api_requires_the_shared_secret_and_one_time_lease() -> None:
    class PatientSource:
        async def fetch_patient(self, _: CallRequest) -> PatientBrief:
            return PatientBrief(quickview_data={}, banner_data={})

    async def scenario() -> None:
        broker = RelayBroker(operation_timeout_seconds=1)
        dispatcher = RelayDispatcher(broker)
        services = CallingServices(
            coordinator=CallCoordinator(
                patient_source=PatientSource(),
                criteria_repository=UnavailableCriteriaRepository(),
                dispatcher=dispatcher,
            ),
            criteria_repository=UnavailableCriteriaRepository(),
            relay_broker=broker,
            relay_secret="r" * 24,
            call_launch_secret="l" * 24,
        )
        app = FastAPI()
        install_calling_router(app, services=services)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # A Daytona backend refuses calls until its local relay polls.
            denied_launch = await client.post(
                "/calls",
                json={
                    "to_phone_number": "+12125550123",
                    "patient_id": "sandbox-patient-id",
                    "payer": "aetna",
                    "plan_type": "commercial",
                    "drug": "pembrolizumab",
                },
            )
            assert denied_launch.status_code == 403
            unavailable = await client.post(
                "/calls",
                headers={"Authorization": "Bearer " + "l" * 24},
                json={
                    "to_phone_number": "+12125550123",
                    "patient_id": "sandbox-patient-id",
                    "payer": "aetna",
                    "plan_type": "commercial",
                    "drug": "pembrolizumab",
                },
            )
            assert unavailable.status_code == 503

            dispatching = asyncio.create_task(dispatcher.dispatch(_request()))
            await asyncio.sleep(0)
            missing_auth = await client.post("/internal/relay/poll")
            assert missing_auth.status_code == 403

            accepted = await client.post(
                "/internal/relay/poll",
                headers={"Authorization": "Bearer " + "r" * 24},
            )
            assert accepted.status_code == 200
            operation = accepted.json()
            assert set(operation) == {
                "operation_id",
                "lease_token",
                "kind",
                "room_name",
                "call_id",
                "agent_name",
                "metadata",
            }

            bad_lease = await client.post(
                f"/internal/relay/operations/{operation['operation_id']}/result",
                headers={"Authorization": "Bearer " + "r" * 24},
                json={"lease_token": "wrong", "succeeded": True},
            )
            assert bad_lease.status_code == 403
            completed = await client.post(
                f"/internal/relay/operations/{operation['operation_id']}/result",
                headers={"Authorization": "Bearer " + "r" * 24},
                json={"lease_token": operation["lease_token"], "succeeded": True},
            )
            assert completed.status_code == 204
            idempotent = await client.post(
                f"/internal/relay/operations/{operation['operation_id']}/result",
                headers={"Authorization": "Bearer " + "r" * 24},
                json={"lease_token": operation["lease_token"], "succeeded": True},
            )
            assert idempotent.status_code == 204
            await dispatching

    asyncio.run(scenario())


def test_daytona_mode_rejects_reusing_the_launch_secret_for_relay_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_secret = "r" * 24
    monkeypatch.setenv("CALL_DISPATCH_MODE", "daytona-relay")
    monkeypatch.setenv("CALL_RELAY_SECRET", shared_secret)
    monkeypatch.setenv("CALL_LAUNCH_SECRET", shared_secret)

    with pytest.raises(RuntimeError, match="must differ"):
        build_calling_services()


def test_leased_dispatch_keeps_the_gate_closed_until_final_timeout_cleanup() -> None:
    class PatientSource:
        async def fetch_patient(self, _: CallRequest) -> PatientBrief:
            return PatientBrief(quickview_data={}, banner_data={})

    def discard(coroutine: object) -> None:
        coroutine.close()

    async def next_operation(broker: RelayBroker):
        for _ in range(20):
            operation = await broker.poll()
            if operation is not None:
                return operation
            await asyncio.sleep(0)
        raise AssertionError("expected a relay operation")

    async def scenario() -> None:
        broker = RelayBroker(operation_timeout_seconds=0.01)
        dispatcher = RelayDispatcher(broker)
        coordinator = CallCoordinator(
            patient_source=PatientSource(),
            criteria_repository=UnavailableCriteriaRepository(),
            dispatcher=dispatcher,
            task_spawner=discard,
        )
        record = await coordinator.create_call(
            CallRequest(
                to_phone_number="+12125550123",
                patient_id="sandbox-patient-id",
                payer="aetna",
                plan_type="commercial",
                drug="pembrolizumab",
            )
        )
        preparing = asyncio.create_task(coordinator.prepare_call(record.call_id))
        dispatch = await next_operation(broker)
        assert dispatch.kind == "create_dispatch"

        # Passing the normal result timeout after leasing must not turn an
        # ambiguous cloud dispatch into a new eligible call.
        await asyncio.sleep(0.02)
        assert not preparing.done()

        expiring = asyncio.create_task(coordinator.expire_call(record.call_id))
        speculative_cleanup = await next_operation(broker)
        assert speculative_cleanup.kind == "delete_room"
        await broker.complete(
            operation_id=speculative_cleanup.operation_id,
            lease_token=speculative_cleanup.lease_token,
            succeeded=True,
        )
        await expiring
        with pytest.raises(ActiveCallError):
            await coordinator.create_call(
                CallRequest(
                    to_phone_number="+12125550123",
                    patient_id="sandbox-patient-id",
                    payer="aetna",
                    plan_type="commercial",
                    drug="pembrolizumab",
                )
            )

        # Once the original relay acknowledgement arrives, the coordinator
        # schedules a final delete because the first was speculative.
        await broker.complete(
            operation_id=dispatch.operation_id,
            lease_token=dispatch.lease_token,
            succeeded=True,
        )
        final_cleanup = await next_operation(broker)
        assert final_cleanup.kind == "delete_room"
        await broker.complete(
            operation_id=final_cleanup.operation_id,
            lease_token=final_cleanup.lease_token,
            succeeded=True,
        )
        await preparing

        next_record = await coordinator.create_call(
            CallRequest(
                to_phone_number="+12125550123",
                patient_id="sandbox-patient-id",
                payer="aetna",
                plan_type="commercial",
                drug="pembrolizumab",
            )
        )
        assert next_record.call_id != record.call_id

    asyncio.run(scenario())


def test_relay_dispatch_failure_is_treated_as_indeterminate() -> None:
    async def scenario() -> None:
        broker = RelayBroker(operation_timeout_seconds=1)
        dispatcher = RelayDispatcher(broker)

        dispatching = asyncio.create_task(dispatcher.dispatch(_request()))
        await asyncio.sleep(0)
        operation = await broker.poll()
        assert operation is not None

        await broker.complete(
            operation_id=operation.operation_id,
            lease_token=operation.lease_token,
            succeeded=False,
        )
        with pytest.raises(IndeterminateDispatchError):
            await dispatching

    asyncio.run(scenario())
