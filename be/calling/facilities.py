"""Location-only facility discovery kept separate from patient/call context."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

MAX_FACILITY_CANDIDATES = 3
TAVILY_TIMEOUT_SECONDS = 6.0
FACILITY_SEARCH_TTL_SECONDS = 10 * 60


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
        """Return locally relevant, callable facility candidates for one ZIP."""


class _TavilyClient(Protocol):
    async def search(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FacilityCandidate:
    """Minimal public data that a human can inspect before authorizing a call."""

    name: str
    phone_e164: str
    source_url: str

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or len(name) > 240:
            raise ValueError("facility candidate name is invalid")
        if not _is_us_e164(self.phone_e164):
            raise ValueError("facility candidate phone is invalid")
        if not _is_public_url(self.source_url):
            raise ValueError("facility candidate source URL is invalid")
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class FacilitySearch:
    """An opaque ID and the candidate snapshot selectable for a short time."""

    search_id: str
    zip_code: str
    candidates: tuple[tuple[str, FacilityCandidate], ...]


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
    """One bounded Tavily request that contains only a validated ZIP code."""

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
            # This is deliberately one basic search: no retry/fan-out and no
            # request receives case, patient, or caller-supplied free text.
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
            candidates=tuple((str(uuid.uuid4()), candidate) for candidate in selected),
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
            for stored_id, candidate in stored.search.candidates:
                if stored_id == candidate_id:
                    return candidate
        raise FacilityCandidateNotFoundError("facility candidate is unavailable")

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

    if not _is_us_zip_code(zip_code):
        raise FacilityDiscoveryError(
            "facility discovery needs a five-digit US ZIP code"
        )
    # `10001` is the current hackathon destination. The city/state terms make
    # search intent unambiguous without deriving anything from a patient case.
    city_state = "New York NY" if zip_code == "10001" else "United States"
    return f"infusion centers near {zip_code} {city_state} official phone number"


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
        if not _is_public_url(source_url):
            continue
        phone = _first_us_phone(
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
    # returned or stored; cap it anyway so a malformed result cannot dominate
    # a local request.
    pieces = [value[:16_000] for value in values if isinstance(value, str)]
    return "\n".join(pieces)


_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?1[.\-\s()]*)?([2-9]\d{2})[.\-\s()]*([2-9]\d{2})[.\-\s]*([0-9]{4})(?!\d)"
)


def _first_us_phone(text: str) -> str | None:
    match = _PHONE_PATTERN.search(text)
    if match is None:
        return None
    phone = "+1" + "".join(match.groups())
    return phone if _is_us_e164(phone) else None


def _is_us_e164(value: str) -> bool:
    return (
        len(value) == 12
        and value.startswith("+1")
        and value[2:].isdigit()
        and value[2] in "23456789"
    )


def _is_us_zip_code(value: str) -> bool:
    return len(value) == 5 and value.isdigit()


def _is_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def _close_client(client: _TavilyClient) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    outcome = close()
    if inspect.isawaitable(outcome):
        await outcome
