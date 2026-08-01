"""Small backend-owned boundary for synthetic patient context."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import CallRequest, PatientBrief


@runtime_checkable
class PatientSource(Protocol):
    """Return the retained context for one validated outbound call request."""

    async def fetch_patient(self, request: CallRequest) -> PatientBrief:
        """Resolve synthetic context without contacting a patient-data service."""
