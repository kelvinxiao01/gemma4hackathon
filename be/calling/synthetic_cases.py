"""Fixture-backed synthetic clinical context for the local outbound demo.

The committed fixture is the sole patient-context source for this hackathon
path.  It is loaded locally and never makes a network request.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import CallRequest, PatientBrief, Payer, PlanType


DEFAULT_CASES_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "cases.json"


class SyntheticCaseError(RuntimeError):
    """Base error for a local synthetic-case lookup."""


class SyntheticCaseConfigurationError(SyntheticCaseError):
    """Raised when the committed fixture cannot be safely loaded."""


class SyntheticCaseNotFoundError(SyntheticCaseError):
    """Raised when a requested fixture case ID does not exist."""


class SyntheticCaseMismatchError(SyntheticCaseError):
    """Raised when request policy fields differ from the selected case."""


@dataclass(frozen=True, slots=True)
class SyntheticCase:
    """Validated fields from one non-identifying committed fixture case."""

    case_id: str
    payer: Payer
    plan_type: PlanType
    drug: str
    patient: dict[str, Any]
    screening: dict[str, Any]
    provider: dict[str, Any]
    form: dict[str, Any]


class SyntheticCasePatientSource:
    """Select a committed synthetic case and turn it into a safe brief."""

    is_ready = True
    readiness_mode = "fixture"

    def __init__(self, *, cases_path: Path = DEFAULT_CASES_PATH) -> None:
        self._cases = _load_cases(cases_path)

    def build_call_request(
        self,
        *,
        to_phone_number: str,
        case_id: str,
    ) -> CallRequest:
        """Build a request whose policy tuple is derived from its case."""

        case = self._case(case_id)
        return CallRequest(
            to_phone_number=to_phone_number,
            patient_id=case.case_id,
            payer=case.payer,
            plan_type=case.plan_type,
            drug=case.drug,
        )

    async def fetch_patient(self, request: CallRequest) -> PatientBrief:
        """Return fresh fixture context only when all request fields agree."""

        case = self._case(request.patient_id)
        if (
            request.payer != case.payer
            or request.plan_type != case.plan_type
            or request.drug.casefold() != case.drug.casefold()
        ):
            raise SyntheticCaseMismatchError(
                "the requested payer, plan type, or drug does not match the case"
            )
        return PatientBrief(
            quickview_data={
                "synthetic": True,
                "case_id": case.case_id,
                "patient": copy.deepcopy(case.patient),
                "screening": copy.deepcopy(case.screening),
                "provider": copy.deepcopy(case.provider),
                "form": copy.deepcopy(case.form),
            },
            banner_data={
                "synthetic": True,
                "case_id": case.case_id,
                "payer": case.payer.value,
                "plan_type": case.plan_type.value,
                "drug": case.drug,
            },
        )

    def _case(self, case_id: str) -> SyntheticCase:
        try:
            return self._cases[case_id]
        except KeyError as exc:
            raise SyntheticCaseNotFoundError("synthetic case is unavailable") from exc


def _load_cases(cases_path: Path) -> dict[str, SyntheticCase]:
    try:
        raw = json.loads(cases_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyntheticCaseConfigurationError(
            "synthetic case fixture is unavailable or malformed"
        ) from exc
    if not isinstance(raw, list):
        raise SyntheticCaseConfigurationError("synthetic case fixture must be a list")

    cases: dict[str, SyntheticCase] = {}
    for item in raw:
        case = _parse_case(item)
        if case.case_id in cases:
            raise SyntheticCaseConfigurationError("synthetic case IDs must be unique")
        cases[case.case_id] = case
    if not cases:
        raise SyntheticCaseConfigurationError("synthetic case fixture must not be empty")
    return cases


def _parse_case(item: object) -> SyntheticCase:
    if not isinstance(item, dict):
        raise SyntheticCaseConfigurationError("synthetic case entries must be objects")
    case_id = _required_string(item, "case_id")
    payer_value = _required_string(item, "payer").casefold()
    plan_type_value = _required_string(item, "plan_type").casefold()
    drug = _required_string(item, "drug").casefold()
    try:
        payer = Payer(payer_value)
        plan_type = PlanType(plan_type_value)
    except ValueError as exc:
        raise SyntheticCaseConfigurationError(
            "synthetic case has an unsupported payer or plan type"
        ) from exc
    return SyntheticCase(
        case_id=case_id,
        payer=payer,
        plan_type=plan_type,
        drug=drug,
        patient=_required_object(item, "patient"),
        screening=_required_object(item, "screening"),
        provider=_required_object(item, "provider"),
        form=_required_object(item, "form"),
    )


def _required_string(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SyntheticCaseConfigurationError(
            f"synthetic case field {key} must be a non-empty string"
        )
    return value.strip()


def _required_object(item: dict[str, object], key: str) -> dict[str, Any]:
    value = item.get(key)
    if not isinstance(value, dict):
        raise SyntheticCaseConfigurationError(
            f"synthetic case field {key} must be an object"
        )
    return value
