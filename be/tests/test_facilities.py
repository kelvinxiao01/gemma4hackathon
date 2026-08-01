"""Public contract tests for local infusion-center discovery and dialing."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from calling.coordinator import CallCoordinator
from calling.criteria import UnavailableCriteriaRepository
from calling.facilities import (
    FacilityCandidate,
    FacilitySearchService,
    TavilyFacilityDiscovery,
)
from calling.router import CallingServices, install_calling_router
from calling.synthetic_cases import SyntheticCasePatientSource
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class FakeDispatcher:
    requests: list[object] = field(default_factory=list)

    async def dispatch(self, request: object) -> None:
        self.requests.append(request)


class DeferredSpawner:
    """Prevents HTTP tests from creating background tasks."""

    def __call__(self, coroutine: object) -> None:
        coroutine.close()


@dataclass
class FakeFacilityDiscovery:
    calls: list[str] = field(default_factory=list)

    async def search(self, *, zip_code: str) -> list[FacilityCandidate]:
        self.calls.append(zip_code)
        return [
            FacilityCandidate(
                name="Chelsea Infusion Center",
                phone_e164="+12125550123",
                source_url="https://vivoinfusion.com/locations/chelsea-infusion",
            ),
            FacilityCandidate(
                name="Midtown Infusion Center",
                phone_e164="+12125550124",
                source_url="https://ivxhealth.com/locations/midtown-infusion",
            ),
            FacilityCandidate(
                name="Hudson Infusion Center",
                phone_e164="+12125550125",
                source_url="https://thrivewellinfusion.com/locations/hudson-infusion",
            ),
        ]


def _services() -> tuple[CallingServices, FakeDispatcher, FakeFacilityDiscovery]:
    case_source = SyntheticCasePatientSource()
    dispatcher = FakeDispatcher()
    coordinator = CallCoordinator(
        patient_source=case_source,
        criteria_repository=UnavailableCriteriaRepository(),
        dispatcher=dispatcher,
        task_spawner=DeferredSpawner(),
    )
    discovery = FakeFacilityDiscovery()
    return (
        CallingServices(
            coordinator=coordinator,
            criteria_repository=UnavailableCriteriaRepository(),
            facility_searches=FacilitySearchService(discovery=discovery),
            case_source=case_source,
            demo_destination="+13478868173",
        ),
        dispatcher,
        discovery,
    )


def _client(services: CallingServices) -> TestClient:
    app = FastAPI()
    install_calling_router(app, services=services)
    return TestClient(app)


def test_searching_10001_returns_three_safe_candidates_without_case_context() -> None:
    services, _, discovery = _services()

    with _client(services) as client:
        response = client.post("/facility-searches", json={"zip_code": "10001"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["zip_code"] == "10001"
    assert len(payload["candidates"]) == 3
    assert discovery.calls == ["10001"]
    assert payload["candidates"][0] == {
        "candidate_id": payload["candidates"][0]["candidate_id"],
        "name": "Chelsea Infusion Center",
        "phone_e164": "+12125550123",
        "source_url": "https://vivoinfusion.com/locations/chelsea-infusion",
    }
    assert "case-002" not in response.text
    assert "synthetic" not in response.text.lower()


def test_facility_search_rejects_non_us_zip_codes_before_calling_tavily() -> None:
    services, _, discovery = _services()

    with _client(services) as client:
        response = client.post("/facility-searches", json={"zip_code": "SW1A 1AA"})

    assert response.status_code == 422
    assert discovery.calls == []


def test_facility_search_is_limited_to_the_configured_10001_demo_area() -> None:
    services, _, discovery = _services()

    with _client(services) as client:
        response = client.post("/facility-searches", json={"zip_code": "10002"})

    assert response.status_code == 422
    assert discovery.calls == []


def test_discovery_snapshot_can_be_read_before_confirming_its_exact_candidate() -> None:
    services, _, _ = _services()

    with _client(services) as client:
        created = client.post("/facility-searches", json={"zip_code": "10001"})
        snapshot = client.get(f"/facility-searches/{created.json()['search_id']}")

    assert snapshot.status_code == 200
    assert snapshot.json() == created.json()


def test_selected_candidate_starts_a_demo_call_and_never_leaks_phone_to_metadata() -> (
    None
):
    services, dispatcher, _ = _services()

    with _client(services) as client:
        discovery = client.post("/facility-searches", json={"zip_code": "10001"})
        candidate_id = discovery.json()["candidates"][0]["candidate_id"]
        refused = client.post(
            f"/facility-searches/{discovery.json()['search_id']}/demo-calls",
            json={
                "candidate_id": candidate_id,
                "case_id": "case-002",
                "confirm_destination": False,
            },
        )
        accepted = client.post(
            f"/facility-searches/{discovery.json()['search_id']}/demo-calls",
            json={
                "candidate_id": candidate_id,
                "case_id": "case-002",
                "confirm_destination": True,
            },
        )

    assert refused.status_code == 422
    assert accepted.status_code == 202

    call_id = accepted.json()["call_id"]
    asyncio.run(services.coordinator.prepare_call(call_id))
    assert len(dispatcher.requests) == 1
    metadata = json.loads(dispatcher.requests[0].metadata)
    assert set(metadata) == {"call_id", "token"}
    assert "+12125550123" not in json.dumps(metadata)
    assert "+13478868173" not in json.dumps(metadata)
    assert "Chelsea Infusion Center" not in json.dumps(metadata)
    context = asyncio.run(
        services.coordinator.internal_context(call_id, metadata["token"])
    )
    assert context.to_phone_number == "+13478868173"
    assert context.recipient.model_dump() == {
        "kind": "infusion-center",
        "name": "Chelsea Infusion Center",
    }


def test_unknown_candidate_cannot_be_confirmed_as_a_call_destination() -> None:
    services, dispatcher, _ = _services()

    with _client(services) as client:
        discovery = client.post("/facility-searches", json={"zip_code": "10001"})
        response = client.post(
            f"/facility-searches/{discovery.json()['search_id']}/demo-calls",
            json={
                "candidate_id": "not-returned-by-this-search",
                "case_id": "case-002",
                "confirm_destination": True,
            },
        )

    assert response.status_code == 404
    assert dispatcher.requests == []


def test_selected_center_is_never_dialed_when_demo_destination_is_missing() -> None:
    services, dispatcher, _ = _services()
    services.demo_destination = None

    with _client(services) as client:
        discovery = client.post("/facility-searches", json={"zip_code": "10001"})
        response = client.post(
            f"/facility-searches/{discovery.json()['search_id']}/demo-calls",
            json={
                "candidate_id": discovery.json()["candidates"][0]["candidate_id"],
                "case_id": "case-002",
                "confirm_destination": True,
            },
        )

    assert response.status_code == 503
    assert dispatcher.requests == []


def test_tavily_query_is_fixed_to_the_zip_and_returns_only_valid_phone_candidates() -> (
    None
):
    class FakeTavilyClient:
        def __init__(self) -> None:
            self.request: dict[str, object] | None = None

        async def search(self, **kwargs: object) -> dict[str, object]:
            self.request = kwargs
            return {
                "results": [
                    {
                        "title": "Chelsea Infusion Center",
                        "url": "https://public-directory.example/chelsea",
                        "content": "Call (212) 555-0123 to schedule an infusion.",
                    },
                    {
                        "title": "Unrelated Clinic",
                        "url": "https://untrusted.example/clinic",
                        "content": "Call (212) 555-0999 for an appointment.",
                    },
                    {
                        "title": "Unlabelled Infusion Center",
                        "url": "https://ivxhealth.com/locations/unlabelled",
                        "content": "For scheduling, use (212) 555-0888.",
                    },
                    {
                        "title": "Loopback Infusion Center",
                        "url": "https://127.0.0.1/local-only",
                        "content": "Phone: (212) 555-0777",
                    },
                    {
                        "title": "Oversized Source Infusion Center",
                        "url": "https://example.com/" + "x" * 2_100,
                        "content": "Phone: (212) 555-0666",
                    },
                ]
            }

        async def close(self) -> None:
            return None

    client = FakeTavilyClient()
    discovery = TavilyFacilityDiscovery(
        api_key="test-key",
        client_factory=lambda _: client,
    )

    candidates = asyncio.run(discovery.search(zip_code="10001"))

    assert client.request == {
        "query": "infusion centers near 10001 New York NY official phone number",
        "search_depth": "basic",
        "max_results": 10,
        "include_raw_content": "text",
        "timeout": 6.0,
    }
    assert candidates == [
        FacilityCandidate(
            name="Chelsea Infusion Center",
            phone_e164="+12125550123",
            source_url="https://public-directory.example/chelsea",
        )
    ]
