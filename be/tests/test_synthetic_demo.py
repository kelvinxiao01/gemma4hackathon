"""Tests for the local fixture-backed outbound-call harness."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace

import pytest
from fastapi.testclient import TestClient

from calling.models import CallOutcome, CallRequest, CallStatus
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
from calling.synthetic_cases import (
    SyntheticCaseConfigurationError,
    SyntheticCasePatientSource,
)


@dataclass
class FakeDispatcher:
    requests: list[object] = field(default_factory=list)
    deleted_rooms: list[str] = field(default_factory=list)

    async def dispatch(self, request: object) -> None:
        self.requests.append(request)

    async def delete_room(self, room_name: str) -> None:
        self.deleted_rooms.append(room_name)


class DeferredSpawner:
    """Avoid background work in an otherwise synchronous unit test."""

    def __call__(self, coroutine: object) -> None:
        coroutine.close()


def test_synthetic_call_request_is_derived_from_the_selected_fixture_case() -> None:
    request = build_synthetic_call_request(
        SyntheticCallOptions(
            to_phone_number="+13478868173",
            case_id="case-002",
        )
    )

    assert request.patient_id == "case-002"
    assert request.to_phone_number == "+13478868173"
    assert request.payer == "aetna"
    assert request.plan_type == "commercial"
    assert request.drug == "ustekinumab"


def test_synthetic_demo_dispatches_from_fixture_and_exposes_only_case_context() -> None:
    dispatcher = FakeDispatcher()
    services = build_synthetic_demo_services(
        livekit_url="wss://example.livekit.cloud",
        api_key="api-key",
        api_secret="api-secret",
        allowed_destination="+13478868173",
        dispatcher=dispatcher,
        task_spawner=DeferredSpawner(),
    )
    request = build_synthetic_call_request(
        SyntheticCallOptions(
            to_phone_number="+13478868173",
            case_id="case-002",
        )
    )

    record = asyncio.run(services.coordinator.create_call(request))
    asyncio.run(services.coordinator.prepare_call(record.call_id))

    assert record.status == CallStatus.DISPATCHED
    assert len(dispatcher.requests) == 1
    metadata = json.loads(dispatcher.requests[0].metadata)
    context = asyncio.run(services.coordinator.internal_context(
        record.call_id, metadata["token"]))
    assert context.patient == {
        "quickview_data": {
            "synthetic": True,
            "case_id": "case-002",
            "patient": {
                "age": 41,
                "weight_kg": 68,
                "height_cm": 163,
                "diagnosis": "moderately to severely active Crohn's disease",
                "treatment_history": [
                    "azathioprine, 8 months, inadequate response",
                    "prednisone, 12 weeks, dependent",
                ],
            },
            "screening": {
                "tb_test": "QuantiFERON-TB Gold, negative, 2026-05-12",
            },
            "provider": {
                "name": "Dr. Robin Fixture",
                "npi": "1234567890",
                "specialty": "gastroenterology",
            },
            "form": {
                "absolute_dose_mg": None,
                "bsa_m2": None,
                "npi_verified": None,
            },
        },
        "banner_data": {
            "synthetic": True,
            "case_id": "case-002",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "ustekinumab",
        },
    }
    assert "contact_data" not in json.dumps(context.patient)


def test_synthetic_demo_rejects_a_case_policy_mismatch_before_dispatch() -> None:
    dispatcher = FakeDispatcher()
    services = build_synthetic_demo_services(
        livekit_url="wss://example.livekit.cloud",
        api_key="api-key",
        api_secret="api-secret",
        allowed_destination="+13478868173",
        dispatcher=dispatcher,
        task_spawner=DeferredSpawner(),
    )
    mismatched_request = CallRequest(
        to_phone_number="+13478868173",
        patient_id="case-002",
        payer="aetna",
        plan_type="commercial",
        drug="pembrolizumab",
    )

    record = asyncio.run(services.coordinator.create_call(mismatched_request))
    asyncio.run(services.coordinator.prepare_call(record.call_id))

    assert record.status == CallStatus.FAILED
    assert record.outcome == CallOutcome.CONTEXT_ERROR
    assert dispatcher.requests == []


def test_synthetic_demo_rejects_an_unknown_case_before_dispatch() -> None:
    dispatcher = FakeDispatcher()
    services = build_synthetic_demo_services(
        livekit_url="wss://example.livekit.cloud",
        api_key="api-key",
        api_secret="api-secret",
        allowed_destination="+13478868173",
        dispatcher=dispatcher,
        task_spawner=DeferredSpawner(),
    )
    unknown_request = CallRequest(
        to_phone_number="+13478868173",
        patient_id="case-missing",
        payer="aetna",
        plan_type="commercial",
        drug="ustekinumab",
    )

    record = asyncio.run(services.coordinator.create_call(unknown_request))
    asyncio.run(services.coordinator.prepare_call(record.call_id))

    assert record.status == CallStatus.FAILED
    assert record.outcome == CallOutcome.CONTEXT_ERROR
    assert dispatcher.requests == []


def test_synthetic_case_source_rejects_malformed_fixture(tmp_path) -> None:
    malformed = tmp_path / "cases.json"
    malformed.write_text("{}", encoding="utf-8")

    with pytest.raises(SyntheticCaseConfigurationError, match="must be a list"):
        SyntheticCasePatientSource(cases_path=malformed)


def test_standard_calling_services_use_fixture_cases_without_patient_service_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import calling.router as calling_router

    dispatcher = FakeDispatcher()
    monkeypatch.setattr(calling_router, "LiveKitDispatcher", lambda **_: dispatcher)
    monkeypatch.setattr(calling_router, "load_dotenv", lambda *_, **__: False)

    services = calling_router.build_calling_services()
    request = build_synthetic_call_request(
        SyntheticCallOptions(
            to_phone_number="+13478868173",
            case_id="case-002",
        )
    )
    record = asyncio.run(services.coordinator.create_call(request))
    asyncio.run(services.coordinator.prepare_call(record.call_id))

    assert record.status == CallStatus.DISPATCHED
    assert len(dispatcher.requests) == 1


def test_required_livekit_config_does_not_require_patient_service_credentials() -> None:
    assert required_livekit_config({
        "LIVEKIT_URL": "wss://example.livekit.cloud",
        "LIVEKIT_API_KEY": "api-key",
        "LIVEKIT_API_SECRET": "api-secret",
    }) == (
        "wss://example.livekit.cloud",
        "api-key",
        "api-secret",
    )


def test_synthetic_demo_blocks_destinations_outside_its_allowlist() -> None:
    dispatcher = FakeDispatcher()
    services = build_synthetic_demo_services(
        livekit_url="wss://example.livekit.cloud",
        api_key="api-key",
        api_secret="api-secret",
        allowed_destination="+13478868173",
        dispatcher=dispatcher,
        task_spawner=DeferredSpawner(),
    )
    app = build_synthetic_demo_app(services)

    with TestClient(app) as client:
        response = client.post("/calls", json={
            "to_phone_number": "+12125550123",
            "patient_id": "case-002",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "ustekinumab",
        })

    assert response.status_code == 403
    assert dispatcher.requests == []


def test_synthetic_demo_app_fails_closed_without_an_allowlisted_destination() -> None:
    services = build_synthetic_demo_services(
        livekit_url="wss://example.livekit.cloud",
        api_key="api-key",
        api_secret="api-secret",
        allowed_destination="+13478868173",
        dispatcher=FakeDispatcher(),
        task_spawner=DeferredSpawner(),
    )

    with pytest.raises(SyntheticDemoConfigurationError):
        build_synthetic_demo_app(replace(services, allowed_destination=None))


def test_synthetic_launcher_requires_explicit_dial_confirmation() -> None:
    with pytest.raises(DialConfirmationRequired):
        require_dial_confirmation(False)

    require_dial_confirmation(True)


def test_synthetic_launcher_recognizes_only_its_own_backend_marker() -> None:
    assert is_synthetic_demo_health({"mode": "synthetic-demo"})
    assert not is_synthetic_demo_health({"mode": "production"})
    assert not is_synthetic_demo_health({"calling": {}})
    assert not is_synthetic_demo_health("synthetic-demo")


def test_required_livekit_config_reports_only_livekit_variables() -> None:
    with pytest.raises(SyntheticDemoConfigurationError) as error:
        required_livekit_config({})

    assert str(error.value) == (
        "missing required LiveKit configuration: LIVEKIT_URL, LIVEKIT_API_KEY, "
        "LIVEKIT_API_SECRET"
    )
