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
from datetime import datetime, timedelta

from . import tracker

FIXTURES = tracker.FIXTURES

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
    epoch = tracker.epoch()
    centres, chosen = _centres_for(drug)
    tracker.append_event(
        patient_id, "center_search",
        f"Searched {len(centres)} infusion centers for {drug}.",
        {"centers": centres, "chosen": chosen["name"] if chosen else None})

    starts_at = _first_slot(patient_id)
    if chosen is None or starts_at is None:
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
