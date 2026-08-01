"""A narrow boundary around the teammate-owned payer-criteria database.

There is intentionally no SQLite implementation here yet.  Its schema and
migrations remain owned by the teammate building that data source; this module
gives the call workflow a stable, read-only interface in the meantime.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import CriteriaEvidence


@runtime_checkable
class CriteriaRepository(Protocol):
    async def find_criteria(
        self,
        *,
        payer: str,
        plan_type: str,
        drug: str,
    ) -> list[CriteriaEvidence]:
        """Return normalized evidence without exposing database details."""


class UnavailableCriteriaRepository:
    """Safe temporary repository until the authoritative schema lands."""

    is_ready = False

    def __init__(self, *, readiness_mode: str = "unavailable") -> None:
        self.readiness_mode = readiness_mode

    async def find_criteria(
        self,
        *,
        payer: str,
        plan_type: str,
        drug: str,
    ) -> list[CriteriaEvidence]:
        # An unavailable database is a Tavily-fallback condition, never a
        # reason to cancel the call.
        return []


def repository_health(repository: CriteriaRepository) -> dict[str, object]:
    """Expose only readiness, never a database path or schema details."""

    return {
        "ready": bool(getattr(repository, "is_ready", False)),
        "mode": str(getattr(repository, "readiness_mode", "unknown")),
    }
