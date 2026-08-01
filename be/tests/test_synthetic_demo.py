"""Tests for the local, Tross-free outbound-call harness."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace

import pytest
from fastapi.testclient import TestClient

from calling.models import CallStatus
from calling.synthetic_demo import (
    SYNTHETIC_PATIENT_ID,
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


def test_synthetic_call_request_has_no_real_patient_identifier() -> None:
    request = build_synthetic_call_request(
        SyntheticCallOptions(
            to_phone_number="+13478868173",
            payer="aetna",
            plan_type="commercial",
            drug="pembrolizumab",
        )
    )

    assert request.patient_id == SYNTHETIC_PATIENT_ID
    assert request.to_phone_number == "+13478868173"
    assert request.payer == "aetna"
    assert request.plan_type == "commercial"


def test_synthetic_demo_dispatches_without_tross_and_exposes_only_fake_context() -> None:
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
            payer="aetna",
            plan_type="commercial",
            drug="pembrolizumab",
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
            "demo_only": True,
            "notice": "Synthetic call test. No patient data is available.",
        },
        "banner_data": {
            "display_name": "Synthetic call-test patient",
            "synthetic": True,
        },
    }
    assert "contact_data" not in json.dumps(context.patient)


def test_required_livekit_config_does_not_require_tross() -> None:
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
            "patient_id": SYNTHETIC_PATIENT_ID,
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "pembrolizumab",
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
