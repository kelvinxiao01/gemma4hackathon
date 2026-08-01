"""Location-only facility discovery kept separate from patient/call context."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.parse import urlparse

from .models import is_us_e164

MAX_FACILITY_CANDIDATES = 3
MAX_SOURCE_URL_LENGTH = 2_048
TAVILY_TIMEOUT_SECONDS = 6.0
FACILITY_SEARCH_TTL_SECONDS = 10 * 60
HACKATHON_ZIP_CODE = "10001"


class FacilityDiscoveryError(RuntimeError):
    """Base error for a recoverable public facility lookup failure."""


class FacilityDiscoveryUnavailableError(FacilityDiscoveryError):
    """Raised when the configured discovery provider cannot be used."""


class FacilitySearchNotFoundError(LookupError):
    """Raised when a search has expired or was never created."""


class FacilityCandidateNotFoundError(LookupError):
    """Raised when a candidate does not belong to its named search."""


class FacilityDiscovery(Protocol):
    async def search(self, *, zip_code: str) -> list[FacilityCandidate]:
        """Return public, locally relevant candidates for one ZIP."""


class _TavilyClient(Protocol):
    async def search(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FacilityCandidate:
    """Public data a human must inspect before authorizing a call.

    The phone number is deterministically extracted from Tavily's public result
    text. It is source-backed, not independently verified as the facility's
    current direct line; the source URL and explicit confirmation are therefore
    mandatory before any call can be launched.
    """

    name: str
    phone_e164: str
    source_url: str

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or len(name) > 240:
            raise ValueError("facility candidate name is invalid")
        if not is_us_e164(self.phone_e164):
            raise ValueError("facility candidate phone is invalid")
        if len(self.source_url) > MAX_SOURCE_URL_LENGTH or not _is_public_url(
            self.source_url
        ):
            raise ValueError("facility candidate source URL is invalid")
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class FacilitySearchCandidate:
    """A candidate paired with the opaque selection ID it was returned under."""

    candidate_id: str
    candidate: FacilityCandidate


@dataclass(frozen=True, slots=True)
class FacilitySearch:
    """An opaque ID and the candidate snapshot selectable for a short time."""

    search_id: str
    zip_code: str
    candidates: tuple[FacilitySearchCandidate, ...]


@dataclass(frozen=True, slots=True)
class _StoredFacilitySearch:
    search: FacilitySearch
    expires_at: float


class UnavailableFacilityDiscovery:
    """A safe default when the backend has no Tavily API key."""

    is_ready = False

    def __init__(self, *, readiness_mode: str = "unconfigured") -> None:
        self.readiness_mode = readiness_mode

    async def search(self, *, zip_code: str) -> list[FacilityCandidate]:
        del zip_code
        raise FacilityDiscoveryUnavailableError("facility discovery is not configured")


class TavilyFacilityDiscovery:
    """One bounded Tavily request that contains only the fixed public ZIP."""

    is_ready = True
    readiness_mode = "tavily"

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = TAVILY_TIMEOUT_SECONDS,
        client_factory: Callable[[str], _TavilyClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        if client_factory is None:
            # Keep the optional provider package out of route/test imports.
            from tavily import AsyncTavilyClient

            client_factory = AsyncTavilyClient
        self._client_factory = client_factory

    async def search(self, *, zip_code: str) -> list[FacilityCandidate]:
        query = build_facility_query(zip_code=zip_code)
        client = self._client_factory(self._api_key)
        try:
            # This is deliberately one basic search: no retry or fan-out, and
            # the request receives no case, patient, or caller-supplied text.
            response = await asyncio.wait_for(
                client.search(
                    query=query,
                    search_depth="basic",
                    max_results=10,
                    include_raw_content="text",
                    timeout=self._timeout_seconds,
                ),
                timeout=self._timeout_seconds + 0.5,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise FacilityDiscoveryUnavailableError(
                "facility discovery timed out"
            ) from exc
        except Exception as exc:
            raise FacilityDiscoveryUnavailableError(
                "facility discovery failed"
            ) from exc
        finally:
            await _close_client(client)

        if not isinstance(response, Mapping):
            raise FacilityDiscoveryUnavailableError(
                "facility discovery returned invalid data"
            )
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise FacilityDiscoveryUnavailableError(
                "facility discovery returned invalid data"
            )
        return _candidates_from_tavily_results(raw_results)


class FacilitySearchService:
    """Retain a user-visible candidate selection without retaining patient data."""

    def __init__(
        self,
        *,
        discovery: FacilityDiscovery,
        ttl_seconds: float = FACILITY_SEARCH_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._discovery = discovery
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._searches: dict[str, _StoredFacilitySearch] = {}
        self._lock = asyncio.Lock()

    async def search(self, *, zip_code: str) -> FacilitySearch:
        candidates = await self._discovery.search(zip_code=zip_code)
        selected = _bounded_unique_candidates(candidates)
        search_id = str(uuid.uuid4())
        search = FacilitySearch(
            search_id=search_id,
            zip_code=zip_code,
            candidates=tuple(
                FacilitySearchCandidate(
                    candidate_id=str(uuid.uuid4()),
                    candidate=candidate,
                )
                for candidate in selected
            ),
        )
        async with self._lock:
            self._prune_expired_locked()
            self._searches[search_id] = _StoredFacilitySearch(
                search=search,
                expires_at=self._clock() + self._ttl_seconds,
            )
        return search

    async def selected_candidate(
        self,
        *,
        search_id: str,
        candidate_id: str,
    ) -> FacilityCandidate:
        async with self._lock:
            self._prune_expired_locked()
            stored = self._searches.get(search_id)
            if stored is None:
                raise FacilitySearchNotFoundError("facility search is unavailable")
            for item in stored.search.candidates:
                if item.candidate_id == candidate_id:
                    return item.candidate
        raise FacilityCandidateNotFoundError("facility candidate is unavailable")

    async def snapshot(self, *, search_id: str) -> FacilitySearch:
        """Return the exact still-live selection a caller is about to confirm."""

        async with self._lock:
            self._prune_expired_locked()
            stored = self._searches.get(search_id)
            if stored is None:
                raise FacilitySearchNotFoundError("facility search is unavailable")
            return stored.search

    def health(self) -> dict[str, object]:
        return {
            "ready": bool(getattr(self._discovery, "is_ready", False)),
            "mode": str(getattr(self._discovery, "readiness_mode", "unknown")),
        }

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        expired = [
            search_id
            for search_id, stored in self._searches.items()
            if stored.expires_at <= now
        ]
        for search_id in expired:
            self._searches.pop(search_id, None)


def build_facility_query(*, zip_code: str) -> str:
    """Build a fixed, location-only search query with no private context."""

    if zip_code != HACKATHON_ZIP_CODE:
        raise FacilityDiscoveryError(
            f"facility discovery is limited to ZIP {HACKATHON_ZIP_CODE}"
        )
    return f"infusion centers near {zip_code} New York NY official phone number"


def _candidates_from_tavily_results(
    results: list[object],
) -> list[FacilityCandidate]:
    candidates: list[FacilityCandidate] = []
    seen_numbers: set[str] = set()
    for result in results:
        if len(candidates) == MAX_FACILITY_CANDIDATES:
            break
        if not isinstance(result, Mapping):
            continue
        name = result.get("title")
        source_url = result.get("url")
        if not isinstance(name, str) or not isinstance(source_url, str):
            continue
        if (
            "infusion" not in name.casefold() or not _is_public_url(source_url)
        ):
            continue
        phone = _published_us_phone(
            _searchable_result_text(result.get("content"), result.get("raw_content"))
        )
        if phone is None or phone in seen_numbers:
            continue
        try:
            candidate = FacilityCandidate(
                name=name,
                phone_e164=phone,
                source_url=source_url,
            )
        except ValueError:
            continue
        seen_numbers.add(phone)
        candidates.append(candidate)
    return candidates


def _bounded_unique_candidates(
    candidates: list[FacilityCandidate],
) -> list[FacilityCandidate]:
    selected: list[FacilityCandidate] = []
    seen_numbers: set[str] = set()
    for candidate in candidates:
        if len(selected) == MAX_FACILITY_CANDIDATES:
            break
        if candidate.phone_e164 in seen_numbers:
            continue
        seen_numbers.add(candidate.phone_e164)
        selected.append(candidate)
    return selected


def _searchable_result_text(*values: object) -> str:
    # Tavily raw content can be large. It is scanned transiently and never
    # returned or stored; cap it so malformed public content cannot dominate a
    # local request.
    pieces = [value[:16_000] for value in values if isinstance(value, str)]
    return "\n".join(pieces)


_PHONE_VALUE_PATTERN = (
    r"(?:\+?1[.\-\s()]*)?\(?([2-9]\d{2})\)?[.\-\s]*"
    r"([2-9]\d{2})[.\-\s]*([0-9]{4})"
)
_PUBLISHED_PHONE_PATTERN = re.compile(
    rf"(?:phone(?:\s+number)?|telephone|tel|call(?:\s+us)?)\s*[:\-–]?\s*"
    rf"{_PHONE_VALUE_PATTERN}",
    re.IGNORECASE,
)


def _published_us_phone(text: str) -> str | None:
    """Extract a number only when the public source explicitly labels it."""

    match = _PUBLISHED_PHONE_PATTERN.search(text)
    if match is None:
        return None
    phone = "+1" + "".join(match.groups())
    return phone if is_us_e164(phone) else None


def _is_public_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        parsed.port
    except ValueError:
        return False
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        return False
    normalized_host = host.rstrip(".").lower()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        return False
    try:
        return ip_address(normalized_host).is_global
    except ValueError:
        # A DNS hostname may later resolve differently, but we never fetch it;
        # reject only clearly local hostname suffixes here.
        return not normalized_host.endswith((".local", ".internal", ".test"))


async def _close_client(client: _TavilyClient) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    outcome = close()
    if inspect.isawaitable(outcome):
        await outcome
