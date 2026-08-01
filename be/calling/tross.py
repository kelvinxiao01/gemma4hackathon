"""Minimal, privacy-preserving adapter for the Athena sandbox endpoint."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .models import PatientBrief


ATHENA_FETCH_PATIENT_URL = "https://api.ontross.com/api/integrations/athena/fetch-patient"
DEFAULT_MAX_CONTEXT_BYTES = 64 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024


class TrossError(RuntimeError):
    """Base class whose messages are safe to expose in a coarse call outcome."""


class TrossConfigurationError(TrossError):
    pass


class TrossRequestError(TrossError):
    pass


class TrossDataError(TrossError):
    pass


class TrossClient:
    """Fetch only the minimum safe context for a single outbound call."""

    def __init__(
        self,
        *,
        api_key: str | None,
        org_id: str | None,
        auth_id: str | None,
        endpoint: str = ATHENA_FETCH_PATIENT_URL,
        timeout: httpx.Timeout | None = None,
        max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._org_id = org_id
        self._auth_id = auth_id
        self._endpoint = endpoint
        self._timeout = timeout or httpx.Timeout(10.0, connect=5.0)
        self._max_context_bytes = max_context_bytes
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    async def fetch_patient(self, patient_id: str) -> PatientBrief:
        if not self._api_key or not self._org_id or not self._auth_id:
            raise TrossConfigurationError("Tross configuration is unavailable")

        request_body = {
            "input": {"patient_id": patient_id, "org_id": self._org_id},
            "auth_id": self._auth_id,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(
                    self._endpoint,
                    headers={"X-API-Key": self._api_key},
                    json=request_body,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            # Never put the response body, patient identifier, or headers into
            # a log/event that could be returned to the local caller.
            raise TrossRequestError("Tross patient lookup failed") from exc

        # Bound the entire raw body before attempting JSON parsing.  The
        # retained quickview/banner subset has its own tighter limit below;
        # this guard prevents a huge discarded field such as contact_data from
        # consuming parser memory first.
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_response_bytes:
                    raise TrossDataError("Tross patient response is too large")
            except ValueError as exc:
                raise TrossDataError("Tross returned malformed patient data") from exc
        raw_body = response.content
        if len(raw_body) > self._max_response_bytes:
            raise TrossDataError("Tross patient response is too large")
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TrossDataError("Tross returned malformed patient data") from exc
        return self._sanitize_payload(payload)

    def _sanitize_payload(self, payload: Any) -> PatientBrief:
        if not isinstance(payload, dict):
            raise TrossDataError("Tross returned malformed patient data")
        if "quickview_data" not in payload or "banner_data" not in payload:
            raise TrossDataError("Tross returned incomplete patient data")

        quickview_data = payload["quickview_data"]
        banner_data = payload["banner_data"]
        if quickview_data is None or banner_data is None:
            raise TrossDataError("Tross returned incomplete patient data")

        # Serializing to JSON both guarantees the values can safely traverse
        # the internal HTTP boundary and enforces the explicit size limit.  Do
        # not copy payload["contact_data"] under any circumstances.
        try:
            safe_payload = json.dumps(
                {"quickview_data": quickview_data, "banner_data": banner_data},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TrossDataError("Tross returned malformed patient data") from exc
        if len(safe_payload) > self._max_context_bytes:
            raise TrossDataError("Tross patient context is too large")

        return PatientBrief(
            quickview_data=quickview_data,
            banner_data=banner_data,
        )
