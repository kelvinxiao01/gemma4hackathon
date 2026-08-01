"""Data contracts for the outbound coverage-call workflow.

The public contracts intentionally omit the patient payload, destination, and
capability token.  Those remain in the in-memory coordinator only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


class Payer(str, Enum):
    AETNA = "aetna"
    ANTHEM = "anthem"
    CIGNA = "cigna"
    UHC = "uhc"


class PlanType(str, Enum):
    COMMERCIAL = "commercial"
    MEDICARE = "medicare"
    MEDICAID = "medicaid"


class RecipientKind(str, Enum):
    """The recipient category controls the worker's opening and call scope."""

    PAYER = "payer"
    INFUSION_CENTER = "infusion-center"


class CallStatus(str, Enum):
    PREPARING = "preparing"
    DISPATCHED = "dispatched"
    DIALING = "dialing"
    SCREENING = "screening"
    ACTIVE = "active"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    FAILED = "failed"


class CallOutcome(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    DECLINED = "declined"
    VOICEMAIL = "voicemail"
    IVR = "ivr"
    UNAVAILABLE = "unavailable"
    NO_ANSWER = "no-answer"
    CARRIER_ERROR = "carrier-error"
    TIMEOUT = "timeout"
    CONTEXT_ERROR = "context-error"
    AGENT_ERROR = "agent-error"


TERMINAL_STATUSES = frozenset({CallStatus.COMPLETED, CallStatus.FAILED})


class CallRequest(BaseModel):
    """The only user-supplied input accepted by the localhost API."""

    to_phone_number: str = Field(min_length=12, max_length=16)
    patient_id: str = Field(min_length=1, max_length=200)
    payer: Payer
    plan_type: PlanType
    drug: str = Field(min_length=1, max_length=160)

    @field_validator("to_phone_number")
    @classmethod
    def validate_us_e164(cls, value: str) -> str:
        return validate_us_e164(value, field_name="to_phone_number")

    @field_validator("patient_id", "drug")
    @classmethod
    def reject_blank_identifiers(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class FacilitySearchRequest(BaseModel):
    """A location-only request for a short-lived local facility search."""

    zip_code: str = Field(min_length=5, max_length=5)

    @field_validator("zip_code")
    @classmethod
    def validate_us_zip_code(cls, value: str) -> str:
        if len(value) != 5 or not value.isdigit():
            raise ValueError("zip_code must be a five-digit US ZIP code")
        return value


class FacilityCandidateResponse(BaseModel):
    """A selectable public facility result; it has no case context."""

    candidate_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=240)
    phone_e164: str
    source_url: str = Field(max_length=2_048)

    @field_validator("phone_e164")
    @classmethod
    def validate_phone_e164(cls, value: str) -> str:
        return validate_us_e164(value, field_name="phone_e164")

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source_url must be an absolute http(s) URL")
        return value


class FacilitySearchResponse(BaseModel):
    search_id: str = Field(min_length=1, max_length=100)
    zip_code: str
    candidates: list[FacilityCandidateResponse] = Field(max_length=3)


class FacilityCallRequest(BaseModel):
    """Select an in-memory result and explicitly approve its outbound call."""

    candidate_id: str = Field(min_length=1, max_length=100)
    case_id: str = Field(min_length=1, max_length=200)
    confirm_destination: Literal[True]

    @field_validator("candidate_id", "case_id")
    @classmethod
    def reject_blank_selection_identifiers(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class CallRecipient(BaseModel):
    """Private destination context supplied only through the capability route."""

    kind: RecipientKind = RecipientKind.PAYER
    name: str | None = Field(default=None, max_length=240)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("recipient name must not be blank")
        return cleaned

    @model_validator(mode="after")
    def require_center_name(self) -> CallRecipient:
        if self.kind == RecipientKind.INFUSION_CENTER and self.name is None:
            raise ValueError("an infusion-center recipient needs a name")
        return self


@dataclass(frozen=True, slots=True)
class CriteriaEvidence:
    """Schema-independent evidence passed from the backend to the worker."""

    text: str
    source_label: str | None = None
    source_url: str | None = None
    effective_date: date | None = None


@dataclass(frozen=True, slots=True)
class PatientBrief:
    """The safe synthetic-case context retained for one agent call."""

    quickview_data: Any
    banner_data: Any


class PublicSourceReference(BaseModel):
    label: str | None = Field(default=None, max_length=240)
    url: str | None = Field(default=None, max_length=2_048)

    @field_validator("url")
    @classmethod
    def validate_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source URL must be an absolute http(s) URL")
        return value


class CallSummary(BaseModel):
    """A deliberately small, transcript-free final result."""

    criteria_summary: list[str] = Field(default_factory=list, max_length=20)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=20)
    public_sources: list[PublicSourceReference] = Field(
        default_factory=list, max_length=12
    )

    @field_validator("criteria_summary", "unresolved_questions")
    @classmethod
    def validate_summary_lines(cls, lines: list[str]) -> list[str]:
        cleaned: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                raise ValueError("summary entries must not be blank")
            if len(line) > 1_000:
                raise ValueError("summary entries must be at most 1000 characters")
            cleaned.append(line)
        return cleaned


class CallAccepted(BaseModel):
    call_id: str
    status: CallStatus
    status_url: str


class CallStatusResponse(BaseModel):
    call_id: str
    status: CallStatus
    destination_masked: str
    created_at: datetime
    updated_at: datetime
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None
    outcome: CallOutcome | None = None
    summary: CallSummary | None = None


class InternalContextResponse(BaseModel):
    """Sent only to the dispatched worker using its one-call capability."""

    call_id: str
    to_phone_number: str
    payer: Payer
    plan_type: PlanType
    drug: str
    recipient: CallRecipient
    patient: dict[str, Any]
    criteria: list[dict[str, Any]]


class AgentEvent(BaseModel):
    status: CallStatus
    outcome: CallOutcome | None = None
    summary: CallSummary | None = None
    # Agent diagnostics are accepted only to let the worker make a terminal
    # callback.  They are never persisted or returned by the public API.
    error: str | None = Field(default=None, max_length=500)


def validate_us_e164(value: str, *, field_name: str) -> str:
    """Accept a canonical US/NANP phone number and nothing else."""

    # Trial-account destination verification remains Twilio's responsibility;
    # this validation only prevents accidental non-US calls and format drift.
    if len(value) != 12 or not value.startswith("+1"):
        raise ValueError(f"{field_name} must be a US E.164 number")
    national = value[2:]
    if not national.isdigit() or national[0] not in "23456789":
        raise ValueError(f"{field_name} must be a US E.164 number")
    return value


def mask_phone_number(number: str) -> str:
    """Leave enough destination context for a local demo without exposing it."""

    return f"+1••••••{number[-4:]}"
