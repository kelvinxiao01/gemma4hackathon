"""Explicit LiveKit agent dispatch with deliberately minimal metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class DispatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    call_id: str
    room_name: str
    metadata: str


class AgentDispatcher(Protocol):
    async def dispatch(self, request: DispatchRequest) -> None:
        """Dispatch the named worker into the supplied room."""


@runtime_checkable
class RoomCleaner(Protocol):
    async def delete_room(self, room_name: str) -> None:
        """Disconnect all participants and remove a dispatched room."""


class LiveKitDispatcher:
    """The backend's sole LiveKit responsibility: dispatch, never dial."""

    def __init__(
        self,
        *,
        url: str | None,
        api_key: str | None,
        api_secret: str | None,
        agent_name: str = "voice",
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        self._agent_name = agent_name

    async def dispatch(self, request: DispatchRequest) -> None:
        if not self._url or not self._api_key or not self._api_secret:
            raise DispatchError("LiveKit dispatch configuration is unavailable")
        self._validate_metadata(request.metadata)

        # Import lazily so unit tests and local status inspection do not need
        # a working LiveKit installation or cloud connection.
        from livekit import api

        client = api.LiveKitAPI(
            self._url,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        try:
            await client.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=self._agent_name,
                    room=request.room_name,
                    metadata=request.metadata,
                )
            )
        except Exception as exc:
            raise DispatchError("LiveKit agent dispatch failed") from exc
        finally:
            await client.aclose()

    async def delete_room(self, room_name: str) -> None:
        """Best-effort timeout cleanup; deleting a room disconnects its SIP leg."""

        if not self._url or not self._api_key or not self._api_secret:
            raise DispatchError("LiveKit dispatch configuration is unavailable")

        from livekit import api

        client = api.LiveKitAPI(
            self._url,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        try:
            await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception as exc:
            raise DispatchError("LiveKit room cleanup failed") from exc
        finally:
            await client.aclose()

    @staticmethod
    def _validate_metadata(metadata: str) -> None:
        """Protect against future call-context fields leaking into metadata."""

        try:
            payload = json.loads(metadata)
        except (TypeError, ValueError) as exc:
            raise DispatchError("LiveKit dispatch metadata is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {"call_id", "token"}:
            raise DispatchError("LiveKit dispatch metadata is invalid")
        if not all(isinstance(payload[key], str) and payload[key]
                   for key in ("call_id", "token")):
            raise DispatchError("LiveKit dispatch metadata is invalid")
