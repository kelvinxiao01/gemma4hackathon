"""What happens after a human presses a button.

Two flows, both on timers so the dashboard sees them arrive the way it would
see real ones: the payer answering a submission, and an agent finding an
infusion centre and phoning it to book a slot.

The call is simulated end to end. Every event, stage and appointment it writes
is the shape the real bridge writes, so swapping the dial in later changes
where the events come from and nothing about what the dashboard renders.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta

import httpx

from . import tracker

FIXTURES = tracker.FIXTURES

# The calling subsystem is mounted on this same app, so the bridge talks to it
# over its public HTTP contract rather than importing it. That keeps the one
# sanctioned dependency on teammate-owned code to a URL.
CALLING_BASE = "http://127.0.0.1:8000"

# Discovery is limited to this ZIP by the subsystem, and reaching Tavily takes
# a few seconds.
SEARCH_ZIP = "10001"
DISCOVERY_TIMEOUT = 30.0

# The synthetic case the call agent reads its brief from. The facility route
# dials the configured demo number regardless, so this supplies context only.
DEMO_CASE_ID = "case-001"

POLL_SECONDS = 2.0
CALL_TIMEOUT = 300.0

# Set CONDUIT_SIMULATE_CALLS to skip discovery and dialing entirely.
SIMULATE = bool(os.environ.get("CONDUIT_SIMULATE_CALLS"))

# The subsystem's status walk, mapped onto the timeline. `dialing` can jump
# straight to `active` and `screening` straight to `completed`, so each status
# is handled on its own rather than as a sequence to wait out.
LIVE_STATUS = {
    "dialing": ("call_dialing", "calling", "Calling {center}..."),
    "screening": ("call_screening", None, "Working through the phone menu."),
    "active": ("call_active", None, "Agent in conversation with {center}."),
    "summarizing": ("call_summarizing", None,
                    "Call wrapping up. Confirming the slot."),
    "completed": ("call_completed", None,
                  "Appointment confirmed with {center}."),
}

# Seconds the simulated payer takes to answer a submission. Configurable
# because a submitted patient whose story is meant to end at the human gate
# must not answer itself while someone is still narrating it. Set
# CONDUIT_PAYER_DELAY high to park every submission at `submitted`.
PAYER_DELAY = float(os.environ.get("CONDUIT_PAYER_DELAY", "8"))

# Paced so the whole booking reads as a live call rather than a jump cut.
CALL_STEPS = [
    (2.0, "call_dialing", "calling", "Calling {center}..."),
    (5.0, "call_active", None, "Agent in conversation with {center}."),
    (12.0, "call_summarizing", None, "Call wrapping up. Confirming the slot."),
    (15.0, "call_completed", "booked", "Appointment confirmed with {center}."),
]

PAYER_REASONING = ("All initial coverage criteria met; single intravenous "
                   "induction dose authorized.")


def _later(delay: float, fn, *args) -> None:
    timer = threading.Timer(delay, fn, args)
    timer.daemon = True
    timer.start()


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


# --------------------------------------------------------------------------
# The payer answering a submission


def start_payer_decision(patient_id: str, payer_name: str) -> None:
    _later(PAYER_DELAY, _payer_decided, patient_id, payer_name,
           tracker.epoch())


def _payer_decided(patient_id: str, payer_name: str, epoch: int) -> None:
    if epoch != tracker.epoch():
        return
    patient = tracker.get_patient(patient_id)
    if patient is None or patient["stage"] != "submitted":
        return
    tracker.write_decision(patient_id, {
        "outcome": "APPROVED", "source": "payer",
        "reasoning": PAYER_REASONING, "decided_at": tracker.now_iso()})
    tracker.append_event(patient_id, "payer_decision",
                         f"{payer_name} approved the request. "
                         f"{PAYER_REASONING}")
    tracker.set_stage(patient_id, "scheduling",
                      "Approved. Ready to book an infusion appointment.")


# --------------------------------------------------------------------------
# Finding a centre and booking a slot


def _centres_for(drug: str) -> tuple[list[dict], dict | None]:
    """Every centre, flagged for whether it offers the drug, nearest first."""
    rows = []
    for centre in _fixture("centers.json"):
        rows.append({"name": centre["name"], "address": centre["address"],
                     "phone": centre["phone"],
                     "distance_mi": centre["distance_mi"],
                     "offers_drug": drug in centre["drugs"]})
    rows.sort(key=lambda c: c["distance_mi"])
    chosen = next((c for c in rows if c["offers_drug"]), None)
    return rows, chosen


def _first_slot(patient_id: str) -> str | None:
    """Earliest slot inside business hours, as an offset-bearing instant.

    The offset comes from the fixture rather than the host clock so a booked
    appointment prints the same wall time the seeded ones do.
    """
    calendar = _fixture("calendar.json")
    hours = calendar["business_hours"]
    offset = calendar["utc_offset"]
    today = datetime.now().date()
    for slot in calendar["slots"].get(patient_id, []):
        if not hours["opens"] <= slot["time"] < hours["closes"]:
            continue
        day = today + timedelta(days=slot["days_ahead"])
        return f"{day.isoformat()}T{slot['time']}:00{offset}"
    return None


def start_scheduling(patient_id: str, drug: str) -> None:
    """Find a center and book it, off the request thread.

    Discovery reaches a third party and the call runs for minutes, so the
    Schedule click returns immediately and everything after it arrives on the
    timeline the way the dashboard already polls for it.
    """
    _later(0, _schedule, patient_id, drug, tracker.epoch())


def _schedule(patient_id: str, drug: str, epoch: int) -> None:
    starts_at = _first_slot(patient_id)
    if starts_at is None:
        tracker.append_event(patient_id, "note",
                             "No open slot inside business hours. A specialist "
                             "will place this booking.")
        return
    if not SIMULATE and _live_schedule(patient_id, drug, starts_at, epoch):
        return
    _simulated_schedule(patient_id, drug, starts_at, epoch)


# --------------------------------------------------------------------------
# The real path: the teammate's discovery and calling subsystem


def _live_schedule(patient_id: str, drug: str, starts_at: str,
                   epoch: int) -> bool:
    """Discover real centers and place a real call. False means fall back.

    Every failure here is a fallback rather than an error the presenter sees.
    Discovery needs a Tavily key, dialing needs a configured destination and
    LiveKit credentials, and any of them can be absent on the night.
    """
    try:
        with httpx.Client(base_url=CALLING_BASE, timeout=DISCOVERY_TIMEOUT) as c:
            found = c.post("/facility-searches",
                           json={"zip_code": SEARCH_ZIP})
            if not found.is_success:
                print(f"[copilot error] facility search: {found.status_code} "
                      f"{found.text[:120]}")
                return False
            search = found.json()
            candidates = search.get("candidates") or []
            if not candidates:
                return False

            centers = [{"name": row["name"], "address": SEARCH_ZIP,
                        "phone": row["phone_e164"], "distance_mi": None,
                        "offers_drug": True, "source_url": row["source_url"]}
                       for row in candidates]
            chosen = candidates[0]

            placed = c.post(
                f"/facility-searches/{search['search_id']}/demo-calls",
                json={"candidate_id": chosen["candidate_id"],
                      "case_id": DEMO_CASE_ID, "confirm_destination": True})
            if placed.status_code == 409:
                tracker.append_event(
                    patient_id, "note",
                    "Another call is still finishing. Schedule again once it "
                    "clears.")
                return True
            if not placed.is_success:
                print(f"[copilot error] demo call: {placed.status_code} "
                      f"{placed.text[:120]}")
                return False
            call_id = placed.json()["call_id"]
    except httpx.HTTPError as exc:
        print(f"[copilot error] calling subsystem: {type(exc).__name__}: {exc}")
        return False

    # Emitted only once the call is placed, so a fallback owns the timeline
    # alone rather than contradicting a discovery that never led to a call.
    tracker.append_event(
        patient_id, "center_search",
        f"Found {len(centers)} infusion centers near {SEARCH_ZIP} for {drug}.",
        {"centers": centers, "chosen": chosen["name"]})
    tracker.append_event(
        patient_id, "slot_identified",
        f"Slot held at {chosen['name']} for {starts_at}.",
        {"center_name": chosen["name"], "starts_at": starts_at})
    tracker.write_call(patient_id, {"call_id": call_id, "status": "preparing",
                                    "outcome": None, "summary": None})
    tracker.append_event(patient_id, "call_created",
                         f"Calling {chosen['name']} to book the appointment.")
    _poll(patient_id, call_id, chosen, starts_at, epoch)
    return True


def _poll(patient_id: str, call_id: str, centre: dict, starts_at: str,
          epoch: int) -> None:
    """Mirror the call's status onto the timeline until it ends."""
    seen = None
    deadline = time.monotonic() + CALL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        if epoch != tracker.epoch():
            return
        try:
            with httpx.Client(base_url=CALLING_BASE, timeout=10.0) as c:
                response = c.get(f"/calls/{call_id}")
            if not response.is_success:
                continue
            call = response.json()
        except httpx.HTTPError as exc:
            print(f"[copilot error] call poll: {type(exc).__name__}: {exc}")
            continue

        status = call.get("status")
        if status == seen:
            continue
        seen = status
        tracker.write_call(patient_id, {
            "call_id": call_id, "status": status,
            "outcome": call.get("outcome"), "summary": call.get("summary")})

        kind, stage, message = LIVE_STATUS.get(status, (None, None, None))
        if kind:
            tracker.append_event(patient_id, kind,
                                 message.format(center=centre["name"]))
        if stage:
            tracker.set_stage(patient_id, stage,
                              message.format(center=centre["name"]))

        if status == "completed":
            _book(patient_id, centre, starts_at, call.get("summary"))
            return
        if status == "failed":
            tracker.append_event(
                patient_id, "call_failed",
                f"The call did not complete ({call.get('outcome')}). "
                "Schedule again to retry.")
            tracker.set_stage(patient_id, "scheduling",
                              "Ready to try the booking again.")
            return
    tracker.append_event(patient_id, "note",
                         "The call is still running. Refresh to follow it.")


def _book(patient_id: str, centre: dict, starts_at: str,
          summary: dict | None) -> None:
    tracker.write_appointment(patient_id, {
        "center_name": centre["name"],
        "address": centre.get("address") or SEARCH_ZIP,
        "phone": centre.get("phone") or centre.get("phone_e164", ""),
        "starts_at": starts_at, "booked_at": tracker.now_iso()})
    for line in (summary or {}).get("criteria_summary", [])[:3]:
        tracker.append_event(patient_id, "note", line)
    tracker.append_event(
        patient_id, "booked",
        f"Appointment booked at {centre['name']} for {starts_at}.")
    tracker.set_stage(patient_id, "booked",
                      f"Appointment confirmed with {centre['name']}.")


# --------------------------------------------------------------------------
# The fallback: the same event sequence, driven by timers


def _simulated_schedule(patient_id: str, drug: str, starts_at: str,
                        epoch: int) -> None:
    centres, chosen = _centres_for(drug)
    tracker.append_event(
        patient_id, "center_search",
        f"Searched {len(centres)} infusion centers for {drug}.",
        {"centers": centres, "chosen": chosen["name"] if chosen else None})

    if chosen is None:
        tracker.append_event(patient_id, "note",
                             "No infusion center with an open slot offers "
                             f"{drug}. A specialist will place this booking.")
        return

    tracker.append_event(patient_id, "slot_identified",
                         f"Slot held at {chosen['name']} for {starts_at}.",
                         {"center_name": chosen["name"],
                          "starts_at": starts_at})
    tracker.write_call(patient_id, {"call_id": f"sim-{patient_id}",
                                    "status": "preparing", "outcome": None,
                                    "summary": None})
    tracker.append_event(patient_id, "call_created",
                         f"[simulated] Placing a call to {chosen['name']}.")
    for delay, kind, stage, message in CALL_STEPS:
        _later(delay, _call_step, patient_id, kind, stage,
               message.format(center=chosen["name"]), chosen, starts_at, epoch)


def _call_step(patient_id: str, kind: str, stage: str | None, message: str,
               centre: dict, starts_at: str, epoch: int) -> None:
    if epoch != tracker.epoch():
        return
    patient = tracker.get_patient(patient_id)
    if patient is None or patient["stage"] in tracker.TERMINAL_STAGES:
        return

    status = kind.removeprefix("call_")
    tracker.write_call(patient_id, {"call_id": f"sim-{patient_id}",
                                    "status": status, "outcome": None,
                                    "summary": None})
    tracker.append_event(patient_id, kind, f"[simulated] {message}")

    if kind == "call_completed":
        tracker.write_appointment(patient_id, {
            "center_name": centre["name"], "address": centre["address"],
            "phone": centre["phone"], "starts_at": starts_at,
            "booked_at": tracker.now_iso()})
        tracker.append_event(
            patient_id, "booked",
            f"Appointment booked at {centre['name']} for {starts_at}.")
    if stage:
        tracker.set_stage(patient_id, stage, message)
