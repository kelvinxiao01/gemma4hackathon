"""Offline tests for the Mac-only Daytona relay."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest

from calling.daytona_relay import (
    DaytonaRelay,
    DaytonaRelayConfigurationError,
    DaytonaRelayProtocolError,
    RemoteRelayOperation,
    build_daytona_relay_app,
    validate_daytona_backend_url,
)
from calling.livekit import DispatchRequest

_BACKEND_URL = "https://8000-demo.preview.daytona.example"
_RELAY_SECRET = "r" * 24
_CALL_ID = "11111111-1111-4111-8111-111111111111"
_ROOM_NAME = "coverage-22222222-2222-4222-8222-222222222222"


@dataclass
class FakeLiveKitDispatcher:
    dispatched: list[DispatchRequest] = field(default_factory=list)
    deleted_rooms: list[str] = field(default_factory=list)

    async def dispatch(self, request: DispatchRequest) -> None:
        self.dispatched.append(request)

    async def delete_room(self, room_name: str) -> None:
        self.deleted_rooms.append(room_name)


def _operation_payload() -> dict[str, str]:
    return {
        "operation_id": "operation-123",
        "lease_token": "lease-123",
        "kind": "create_dispatch",
        "room_name": _ROOM_NAME,
        "call_id": _CALL_ID,
        "agent_name": "voice",
        "metadata": json.dumps(
            {"call_id": _CALL_ID, "token": "per-call-capability"},
            separators=(",", ":"),
        ),
    }


def test_preview_url_must_be_a_bare_https_url() -> None:
    assert validate_daytona_backend_url(_BACKEND_URL + "/") == _BACKEND_URL
    for invalid in (
        "http://demo.example",
        "https://user:password@demo.example",
        "https://demo.example/api",
        "https://demo.example/?token=secret",
    ):
        with pytest.raises(DaytonaRelayConfigurationError):
            validate_daytona_backend_url(invalid)


def test_remote_operation_rejects_context_and_unknown_agent_fields() -> None:
    payload = _operation_payload()
    payload["patient"] = "must-not-cross-the-relay"
    with pytest.raises(DaytonaRelayProtocolError):
        RemoteRelayOperation.from_payload(payload)

    payload = _operation_payload()
    payload["operation_id"] = "operation/that-is-not-a-token"
    with pytest.raises(DaytonaRelayProtocolError):
        RemoteRelayOperation.from_payload(payload)

    payload = _operation_payload()
    payload["metadata"] = json.dumps(
        {"call_id": "33333333-3333-4333-8333-333333333333", "token": "cap"}
    )
    with pytest.raises(DaytonaRelayProtocolError):
        RemoteRelayOperation.from_payload(payload)

    payload = _operation_payload()
    payload["agent_name"] = "different-agent"
    with pytest.raises(DaytonaRelayProtocolError):
        RemoteRelayOperation.from_payload(payload)


def test_relay_executes_only_the_leased_dispatch_and_reports_its_result() -> None:
    async def scenario() -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.headers["Authorization"] == f"Bearer {_RELAY_SECRET}"
            if request.url.path == "/internal/relay/poll":
                return httpx.Response(200, json=_operation_payload())
            if request.url.path.endswith("/result"):
                assert json.loads(request.content) == {
                    "lease_token": "lease-123",
                    "succeeded": True,
                }
                return httpx.Response(204)
            raise AssertionError(f"unexpected relay route: {request.url.path}")

        dispatcher = FakeLiveKitDispatcher()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_BACKEND_URL
        ) as remote_client:
            relay = DaytonaRelay(
                backend_url=_BACKEND_URL,
                relay_secret=_RELAY_SECRET,
                livekit_dispatcher=dispatcher,
                client=remote_client,
            )
            assert await relay.poll_once()

        assert [request.url.path for request in calls] == [
            "/internal/relay/poll",
            "/internal/relay/operations/operation-123/result",
        ]
        assert dispatcher.dispatched == [
            DispatchRequest(
                call_id=_CALL_ID,
                room_name=_ROOM_NAME,
                metadata=_operation_payload()["metadata"],
            )
        ]

    asyncio.run(scenario())


def test_relay_executes_room_cleanup_operations_without_dispatch_metadata() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/internal/relay/poll":
                return httpx.Response(
                    200,
                    json={
                        "operation_id": "cleanup-123",
                        "lease_token": "cleanup-lease",
                        "kind": "delete_room",
                        "room_name": _ROOM_NAME,
                    },
                )
            if request.url.path.endswith("/result"):
                assert json.loads(request.content)["succeeded"] is True
                return httpx.Response(204)
            raise AssertionError(f"unexpected relay route: {request.url.path}")

        dispatcher = FakeLiveKitDispatcher()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_BACKEND_URL
        ) as remote_client:
            relay = DaytonaRelay(
                backend_url=_BACKEND_URL,
                relay_secret=_RELAY_SECRET,
                livekit_dispatcher=dispatcher,
                client=remote_client,
            )
            assert await relay.poll_once()

        assert dispatcher.dispatched == []
        assert dispatcher.deleted_rooms == [_ROOM_NAME]

    asyncio.run(scenario())


def test_result_acknowledgement_retries_without_reexecuting_dispatch() -> None:
    async def scenario() -> None:
        poll_count = 0
        result_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal poll_count, result_count
            if request.url.path == "/internal/relay/poll":
                poll_count += 1
                return httpx.Response(200, json=_operation_payload())
            if request.url.path.endswith("/result"):
                result_count += 1
                if result_count == 1:
                    raise httpx.ConnectError("acknowledgement interrupted", request=request)
                return httpx.Response(204)
            raise AssertionError(f"unexpected relay route: {request.url.path}")

        dispatcher = FakeLiveKitDispatcher()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_BACKEND_URL
        ) as remote_client:
            relay = DaytonaRelay(
                backend_url=_BACKEND_URL,
                relay_secret=_RELAY_SECRET,
                livekit_dispatcher=dispatcher,
                client=remote_client,
            )
            with pytest.raises(httpx.ConnectError):
                await relay.poll_once()
            assert await relay.poll_once()

        assert poll_count == 1
        assert result_count == 2
        assert len(dispatcher.dispatched) == 1

    asyncio.run(scenario())


def test_loopback_app_proxies_only_worker_context_and_events() -> None:
    async def scenario() -> None:
        received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received.append(request)
            assert request.headers.get("X-Call-Context-Token") == "call-capability"
            if request.method == "GET":
                return httpx.Response(200, json={"call_id": "call-123"})
            if request.method == "POST":
                assert json.loads(request.content) == {"status": "dialing"}
                return httpx.Response(200, json={"status": "dialing"})
            raise AssertionError("unexpected method")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_BACKEND_URL
        ) as remote_client:
            relay = DaytonaRelay(
                backend_url=_BACKEND_URL,
                relay_secret=_RELAY_SECRET,
                livekit_dispatcher=FakeLiveKitDispatcher(),
                client=remote_client,
            )
            app = build_daytona_relay_app(relay)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8010",
            ) as worker:
                context = await worker.get(
                    "/internal/calls/call-123/context",
                    headers={"X-Call-Context-Token": "call-capability"},
                )
                event = await worker.post(
                    "/internal/calls/call-123/events",
                    headers={"X-Call-Context-Token": "call-capability"},
                    json={"status": "dialing"},
                )
                other = await worker.get("/anything-else")

        assert context.status_code == 200
        assert context.json() == {"call_id": "call-123"}
        assert event.status_code == 200
        assert other.status_code == 404
        assert [request.url.path for request in received] == [
            "/internal/calls/call-123/context",
            "/internal/calls/call-123/events",
        ]

    asyncio.run(scenario())
