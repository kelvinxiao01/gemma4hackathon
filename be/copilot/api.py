"""HTTP surface for the copilot. Mounted on the same app as /search."""

import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from docsearch.store import DB_PATH as INDEX_DB_PATH

from . import llm, orchestrate, qualify, tracker

router = APIRouter(prefix="/copilot", tags=["copilot"])

# Qualification is allowed only before a determination exists. Re-running it on
# a submitted or booked patient would rewrite a receipt someone has read.
QUALIFY_STAGES = frozenset({"intake", "policy_matched"})


class SubmitBody(BaseModel):
    action: Literal["submit", "do_not_submit"]


class EventBody(BaseModel):
    kind: str
    message: str
    payload: dict | None = None


def _require(patient_id: str) -> dict:
    patient = tracker.get_patient(patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    return patient


@router.get("/patients")
def list_patients():
    return tracker.list_patients()


@router.get("/patients/{patient_id}")
def get_patient(patient_id: str):
    return _require(patient_id)


@router.post("/patients/{patient_id}/qualify")
def qualify_patient(patient_id: str):
    patient = _require(patient_id)
    # One conditional UPDATE claims the patient, so a double-click cannot put
    # two qualifications on the same timeline. The claim also has to land
    # before the response starts, because once the first chunk is on the wire
    # the status code can no longer be changed.
    epoch = tracker.epoch()
    if not tracker.claim(patient_id, tuple(QUALIFY_STAGES), [
            ("policy_matched",
             f"Policy matched: {patient['payer_name']} "
             f"{patient['plan_type']} {patient['drug']} coverage criteria."),
            ("qualifying",
             "Reading the payer policy against the patient record.")]):
        raise HTTPException(
            status_code=409,
            detail=f"qualification already ran; patient is at stage "
                   f"{patient['stage']}")
    # The determination is written by a background task, not at the tail of
    # the generator: Starlette abandons the generator when the client
    # disconnects, and the tail would then fire late or never.
    sink: dict = {}
    return StreamingResponse(
        qualify.run(patient, sink),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
        background=BackgroundTask(qualify.finalize, patient, epoch, sink))


@router.post("/patients/{patient_id}/submit")
def submit(patient_id: str, body: SubmitBody):
    patient = _require(patient_id)
    if patient["stage"] != "review":
        raise HTTPException(
            status_code=409,
            detail=f"submission needs a patient awaiting review; this one is "
                   f"at stage {patient['stage']}")

    if body.action == "submit":
        tracker.append_event(
            patient_id, "submitted",
            "Clinical criteria passed. Prior authorization request submitted "
            f"to {patient['payer_name']}.")
        tracker.set_stage(patient_id, "submitted",
                          f"Awaiting a decision from {patient['payer_name']}.")
        orchestrate.start_payer_decision(patient_id, patient["payer_name"])
        return {"ok": True, "stage": "submitted"}

    criteria = patient["result"]["criteria"]
    # The first unresolved requirement, contradicted or merely undocumented:
    # at stage `review` a contradiction is impossible, so restricting this to
    # NOT_MET would print the generic line on every real call.
    unresolved = next((c for c in criteria if c["kind"] != "supporting"
                       and c["status"] != "MET"), None)
    reasoning = (f"{unresolved['criterion']}. {unresolved['evidence']}"
                 if unresolved else
                 "Specialist elected not to submit this request.")
    tracker.not_submitted(patient_id, reasoning)
    return {"ok": True, "stage": "not_qualified"}


@router.post("/patients/{patient_id}/schedule")
def schedule(patient_id: str):
    patient = _require(patient_id)
    decision = patient["decision"] or {}
    if patient["stage"] != "scheduling" or decision.get("outcome") != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail="scheduling needs an approved patient waiting on an "
                   f"appointment; this one is at stage {patient['stage']}")
    call = patient["call"] or {}
    if call.get("status") not in (None, "completed", "failed"):
        raise HTTPException(
            status_code=409,
            detail="a call for this patient is already in progress; it "
                   "finishes or times out before another can start")
    orchestrate.start_scheduling(patient_id, patient["drug"])
    return {"ok": True}


@router.post("/patients/{patient_id}/events")
def add_event(patient_id: str, body: EventBody):
    _require(patient_id)
    tracker.append_event(patient_id, body.kind, body.message, body.payload)
    return {"ok": True}


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: str):
    payload = tracker.get_evidence(evidence_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return payload


@router.get("/health")
def health():
    return {"gemma": llm.ping(), "search": _search_health()}


@router.post("/reset")
def reset():
    tracker.reset()
    return {"ok": True}


def _search_health() -> str:
    try:
        # Read-only URI rather than the store's own connect helper: that one
        # creates the database when it is missing, which would turn a health
        # check into a filesystem mutation that reports a healthy empty index.
        conn = sqlite3.connect(f"file:{INDEX_DB_PATH}?mode=ro", uri=True)
        try:
            chunks, = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[copilot error] search health: {type(exc).__name__}: {exc}")
        return "unavailable"
    return "ok" if chunks > 0 else "unavailable"
