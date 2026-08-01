"""Capability-protected communication with the local call coordinator.

The voice worker receives only a call identifier and single-use capability in
LiveKit dispatch metadata.  Full call context stays in the local coordinator
until this module fetches it over the loopback-only internal API.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx


class DispatchMetadataError(ValueError):
    """Dispatch metadata was malformed or contained call context."""


class BackendCallError(RuntimeError):
    """The local coordinator could not be reached or rejected a request."""


class BackendConfigurationError(ValueError):
    """The worker was configured to send call data somewhere unsafe."""


@dataclass(frozen=True)
class DispatchMetadata:
    call_id: str
    token: str


@dataclass(frozen=True)
class CriteriaEvidence:
    text: str
    source_label: str | None = None
    source_url: str | None = None
    effective_date: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CriteriaEvidence:
        text = _required_string(payload, "text")
        return cls(
            text=text,
            source_label=_optional_string(payload.get("source_label")),
            source_url=_optional_string(payload.get("source_url")),
            effective_date=_optional_string(payload.get("effective_date")),
        )


@dataclass(frozen=True)
class OutboundCallContext:
    """The minimum context an outbound worker may use during a call."""

    call_id: str
    to_phone_number: str
    payer: str
    plan_type: str
    drug: str
    patient: dict[str, Any]
    criteria: tuple[CriteriaEvidence, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OutboundCallContext:
        patient_payload = payload.get("patient", {})
        if not isinstance(patient_payload, Mapping):
            raise BackendCallError("backend context has invalid patient data")

        # contact_data must not cross the worker boundary, even if a bad backend
        # response accidentally includes it.
        patient = {
            key: value
            for key in ("quickview_data", "banner_data")
            if (value := patient_payload.get(key)) is not None
        }

        raw_criteria = payload.get("criteria", [])
        if not isinstance(raw_criteria, Sequence) or isinstance(raw_criteria, str):
            raise BackendCallError("backend context has invalid criteria")
        criteria: list[CriteriaEvidence] = []
        for item in raw_criteria:
            if not isinstance(item, Mapping):
                raise BackendCallError("backend context has invalid criteria")
            criteria.append(CriteriaEvidence.from_payload(item))

        return cls(
            call_id=_required_string(payload, "call_id"),
            to_phone_number=_required_string(payload, "to_phone_number"),
            payer=_required_string(payload, "payer").lower(),
            plan_type=_required_string(payload, "plan_type").lower(),
            drug=_required_string(payload, "drug"),
            patient=patient,
            criteria=tuple(criteria),
        )


@dataclass(frozen=True)
class CallSummary:
    criteria_summary: list[str] | None = None
    unresolved_questions: list[str] | None = None
    public_sources: list[dict[str, str]] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.criteria_summary is not None:
            payload["criteria_summary"] = list(self.criteria_summary)
        if self.unresolved_questions is not None:
            payload["unresolved_questions"] = list(self.unresolved_questions)
        if self.public_sources is not None:
            payload["public_sources"] = [dict(source) for source in self.public_sources]
        return payload


def criteria_needs_public_fallback(criteria: Sequence[CriteriaEvidence]) -> bool:
    """Whether local evidence cannot stand alone as a traceable policy source."""

    return not criteria or any(
        not evidence.text.strip() or not evidence.source_url for evidence in criteria
    )


def require_loopback_http_url(base_url: str) -> str:
    """Allow internal callbacks only to an IP-literal loopback HTTP endpoint."""

    try:
        parsed = urlsplit(base_url)
    except (TypeError, ValueError) as exc:
        raise BackendConfigurationError("CALL_BACKEND_URL is invalid") from exc

    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BackendConfigurationError("CALL_BACKEND_URL must be loopback HTTP")
    try:
        is_loopback = ip_address(parsed.hostname).is_loopback
    except ValueError as exc:
        raise BackendConfigurationError(
            "CALL_BACKEND_URL must use a loopback IP"
        ) from exc
    if not is_loopback:
        raise BackendConfigurationError("CALL_BACKEND_URL must use a loopback IP")
    return base_url.rstrip("/")


def parse_dispatch_metadata(raw_metadata: str | None) -> DispatchMetadata:
    """Parse the deliberately tiny dispatch payload.

    Any extra value is rejected rather than silently accepted.  That makes it
    impossible for destination, patient, or criteria data to become LiveKit job
    metadata through this worker.
    """

    if not raw_metadata:
        raise DispatchMetadataError("dispatch metadata is missing")
    try:
        payload = json.loads(raw_metadata)
    except json.JSONDecodeError as exc:
        raise DispatchMetadataError("dispatch metadata is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise DispatchMetadataError("dispatch metadata must be an object")

    allowed_fields = {"call_id", "token"}
    if set(payload) != allowed_fields:
        raise DispatchMetadataError("dispatch metadata contains unsupported fields")
    return DispatchMetadata(
        call_id=_required_string(payload, "call_id", metadata=True),
        token=_required_string(payload, "token", metadata=True),
    )


class BackendCallClient:
    """Small HTTP client that never accepts or transmits transcripts."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 5.0,
        transport: Any | None = None,
    ) -> None:
        self._base_url = require_loopback_http_url(base_url)
        self._token = token
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def fetch_context(self, call_id: str) -> OutboundCallContext:
        response = await self._request("GET", f"/internal/calls/{call_id}/context")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendCallError("backend returned invalid context") from exc
        if not isinstance(payload, Mapping):
            raise BackendCallError("backend returned invalid context")
        context = OutboundCallContext.from_payload(payload)
        if context.call_id != call_id:
            raise BackendCallError("backend returned context for another call")
        return context

    async def report_event(
        self,
        call_id: str,
        *,
        status: str,
        outcome: str | None = None,
        summary: CallSummary | None = None,
        error: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"status": status}
        if outcome is not None:
            payload["outcome"] = outcome
        if summary is not None:
            payload["summary"] = summary.to_payload()
        if error is not None:
            payload["error"] = error
        await self._request("POST", f"/internal/calls/{call_id}/events", json=payload)

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                headers={"X-Call-Context-Token": self._token},
            ) as client:
                response = await client.request(method, path, json=json)
                response.raise_for_status()
                return response
        except httpx.HTTPError as exc:
            raise BackendCallError("backend call request failed") from exc


def _required_string(
    payload: Mapping[str, Any], key: str, *, metadata: bool = False
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        error_type = DispatchMetadataError if metadata else BackendCallError
        raise error_type(f"missing or invalid {key}")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BackendCallError("backend context contains an invalid source field")
    return value.strip() or None
