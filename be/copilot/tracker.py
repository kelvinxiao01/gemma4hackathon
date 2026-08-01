"""Patient pipeline state.

A separate sqlite file from the policy index on purpose: rebuilding the index
means deleting `docsearch.db`, and patient state must not be collateral of
that instruction.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from .score import SCORE_RULE, score_criteria

BASE = Path(__file__).resolve().parent.parent
# Absolute, like the index path: a process started from the repo root would
# otherwise create an empty tracker beside itself and serve zero patients.
DB_PATH = BASE / "data" / "tracker.db"
FIXTURES = BASE / "fixtures"
CORPUS = BASE / "corpus"

TERMINAL_STAGES = frozenset({"booked", "not_qualified", "payer_denied"})

DISCLAIMER = "Decision support for human review. Not a coverage determination."

# The receipt headline. Derived from the band, never from generated text, and
# phrased so no pre-submission outcome reads as a payer denial.
DETERMINATION_TEXT = {
    "QUALIFIES": "Meets the payer's published criteria for submission",
    "NEEDS_REVIEW": "Unresolved criteria, routed to specialist review",
    "NOT_QUALIFIED": "Not submitted, criteria not met",
}

# Closing line for a seeded case that was already finished before the demo.
_TERMINAL_MESSAGE = {
    "booked": "Appointment confirmed. Case complete.",
    "not_qualified": "Case closed without submission.",
    "payer_denied": "Case closed on the payer's determination.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  payer TEXT NOT NULL, payer_name TEXT NOT NULL,
  drug TEXT NOT NULL, brand_name TEXT, plan_type TEXT NOT NULL,
  stage TEXT NOT NULL, score INTEGER, band TEXT,
  record_json TEXT NOT NULL, result_json TEXT,
  decision_json TEXT, appointment_json TEXT, call_json TEXT,
  evidence_id TEXT,
  created_at TEXT NOT NULL, decided_at TEXT, booked_at TEXT,
  updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, patient_id TEXT NOT NULL,
  created_at TEXT NOT NULL, payload_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
  ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
  payload_json TEXT);

CREATE INDEX IF NOT EXISTS events_by_patient ON events(patient_id, id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def _dumps(value) -> str | None:
    return None if value is None else json.dumps(value)


def _loads(raw: str | None):
    return None if raw is None else json.loads(raw)


# Bumped whenever the caseload is wiped and reseeded. Deferred writers capture
# it before they start and drop their write if it moved, so a stream or a timer
# that outlives a reset cannot land on the patient that replaced its own.
_EPOCH = 0

# Serialises first touch of a fresh database. Without it, concurrent openers
# both find `patients` empty and both seed.
_INIT = threading.Lock()


def epoch() -> int:
    return _EPOCH


def _connect() -> sqlite3.Connection:
    # ponytail: one connection per call, opened and closed, copied from
    # docsearch.store. It sidesteps thread affinity entirely under Starlette's
    # threadpool and any background writer. Revisit only under real concurrent
    # load, which would need its own transaction scope anyway.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    with _INIT:
        # WAL before the schema write. The other order leaves the first
        # connections racing in rollback-journal mode, where sqlite skips the
        # busy handler on a lock upgrade and `timeout` never applies. It also
        # lets the dashboard's reads proceed against a concurrent write rather
        # than failing on the lock.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        if not conn.execute("SELECT 1 FROM patients LIMIT 1").fetchone():
            _seed(conn)
    return conn


# --------------------------------------------------------------------------
# Seeding


@lru_cache(maxsize=None)
def policy_doc(payer: str, drug: str, plan_type: str) -> dict:
    """The corpus document behind a patient's citations.

    Read from the corpus rather than repeated in the fixtures so a receipt's
    `source_sha256` can never drift from the document it claims to quote.
    """
    raw = json.loads((CORPUS / f"{payer}_{drug}_{plan_type}.json").read_text())
    return {"doc_url": raw["doc_url"], "source_sha256": raw["source_sha256"],
            "payer_name": raw["payer_name"], "plan_type": raw["plan_type"]}


def _expand_criteria(fixture_criteria: list[dict], doc: dict) -> list[dict]:
    """Give a fixture criterion the same Citation shape a live one carries.

    A null `ref` means the source document has no pagination, so the citation
    falls back to the document URL exactly as a live citation does.
    """
    out = []
    for c in fixture_criteria:
        out.append({
            "criterion": c["criterion"], "kind": c["kind"],
            "status": c["status"], "evidence": c["evidence"],
            "citation": {
                "payer_name": doc["payer_name"], "plan_type": doc["plan_type"],
                "ref": c["ref"] or doc["doc_url"], "doc_url": doc["doc_url"],
                "quote": c["quote"],
            },
        })
    return out


def _seed(conn: sqlite3.Connection) -> None:
    fixtures = json.loads((FIXTURES / "patients.json").read_text())
    now = datetime.now(timezone.utc)
    for p in fixtures:
        # Fixtures carry offsets, not dates, so the caseload reads as live
        # work whichever day the demo runs.
        created = now - timedelta(hours=p["created_hours_ago"])
        decided = (now - timedelta(hours=p["decided_hours_ago"])
                   if "decided_hours_ago" in p else None)
        booked = (now - timedelta(hours=p["booked_hours_ago"])
                  if "booked_hours_ago" in p else None)

        # The fixtures hold offsets, so the exact instants are only known here.
        decision = p.get("decision")
        if decision:
            decision = {**decision, "decided_at": _iso(decided or created)}
        appointment = p.get("appointment")
        if appointment:
            appointment = {**appointment, "booked_at": _iso(booked or created)}

        result = evidence_id = None
        score = band = None
        if p.get("criteria"):
            doc = policy_doc(p["payer"], p["drug"], p["plan_type"])
            criteria = _expand_criteria(p["criteria"], doc)
            score, band = score_criteria(criteria)
            result = {"score": score, "band": band, "criteria": criteria,
                      "summary": p["summary"]}
            evidence_id = _insert_evidence(
                conn, p["id"], receipt(p["id"], p["name"], result, doc,
                                       "seeded", _iso(decided or created)))

        conn.execute(
            "INSERT INTO patients (id, name, payer, payer_name, drug, "
            "brand_name, plan_type, stage, score, band, record_json, "
            "result_json, decision_json, appointment_json, call_json, "
            "evidence_id, created_at, decided_at, booked_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["name"], p["payer"], p["payer_name"], p["drug"],
             p["brand_name"], p["plan_type"], p["stage"], score, band,
             json.dumps(p["record"]), _dumps(result), _dumps(decision),
             _dumps(appointment), None, evidence_id,
             _iso(created), _iso(decided) if decided else None,
             _iso(booked) if booked else None,
             _iso(booked or decided or created)))
        _seed_events(conn, p, created, decided, booked)
    conn.commit()


def _seed_events(conn: sqlite3.Connection, p: dict, created: datetime,
                 decided: datetime | None, booked: datetime | None) -> None:
    """A plausible arc for a case that was worked before the demo started.

    Live patients get one line; a completed card is opened during the demo and
    an empty timeline there reads as a broken page, not as seeded data.
    """
    end = booked or decided or created
    span = (end - created) / 4
    marks = [created + span * i for i in range(5)]
    decision = p.get("decision") or {}

    # A `stage_change` carries the stage it moved to, matching what `set_stage`
    # writes live. A timeline keyed on that payload would otherwise throw on
    # every seeded card and work only on the two qualified during the demo.
    rows = [(marks[0], "stage_change", "Case created at intake.", "intake")]
    if p.get("criteria"):
        rows.append((marks[1], "stage_change",
                     f"Policy matched: {p['payer_name']} {p['plan_type']} "
                     f"{p['drug']} coverage criteria.", "policy_matched"))
        rows.append((marks[2], "qualify_done", p["summary"], None))
    # Decision rows are stamped from the real instant rather than interpolated,
    # so the timeline and the card's decided_at agree on screen.
    if decision.get("source") == "payer":
        rows.append((marks[3], "submitted",
                     "Prior authorization request submitted to "
                     f"{p['payer_name']}.", None))
        rows.append((decided or end, "payer_decision", decision["reasoning"],
                     None))
    elif decision.get("outcome") == "NOT_SUBMITTED":
        rows.append((decided or end, "stage_change",
                     f"Not submitted. {decision['reasoning']}", p["stage"]))
    if p.get("appointment"):
        appt = p["appointment"]
        rows.append((booked or end, "booked", f"Appointment booked at "
                     f"{appt['center_name']} for {appt['starts_at']}.", None))
    # The stage the card actually rests at, so a stepper driven by the timeline
    # ends where the card says it does.
    if p["stage"] in TERMINAL_STAGES and rows[-1][3] != p["stage"]:
        rows.append((booked or decided or end, "stage_change",
                     _TERMINAL_MESSAGE[p["stage"]], p["stage"]))

    for ts, kind, message, stage in rows:
        _append_event(conn, p["id"], kind, message,
                      {"stage": stage} if stage else None, ts=_iso(ts))


def receipt(patient_id: str, name: str, result: dict, doc: dict, model: str,
            created_at: str) -> dict:
    return {
        "patient_id": patient_id, "patient_name": name,
        "determination": DETERMINATION_TEXT[result["band"]],
        "score": result["score"], "band": result["band"],
        "score_rule": SCORE_RULE, "criteria": result["criteria"],
        "doc_sha256": doc["source_sha256"], "doc_url": doc["doc_url"],
        "model": model, "created_at": created_at, "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------
# Reads


def _card(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "payer": row["payer"],
        "payer_name": row["payer_name"], "drug": row["drug"],
        "brand_name": row["brand_name"], "plan_type": row["plan_type"],
        "stage": row["stage"], "score": row["score"], "band": row["band"],
        "decision": _loads(row["decision_json"]),
        "appointment": _loads(row["appointment_json"]),
        "evidence_id": row["evidence_id"], "created_at": row["created_at"],
        "decided_at": row["decided_at"], "booked_at": row["booked_at"],
        "updated_at": row["updated_at"],
    }


def list_patients() -> dict:
    conn = _connect()
    try:
        cards = [_card(r) for r in conn.execute("SELECT * FROM patients")]
    finally:
        conn.close()
    active = [c for c in cards if c["stage"] not in TERMINAL_STAGES]
    completed = [c for c in cards if c["stage"] in TERMINAL_STAGES]
    # Active keeps a stable intake order so cards do not reshuffle under the
    # presenter mid-demo; completed leads with the newest determination so a
    # card that finishes on stage arrives at the top of the list.
    active.sort(key=lambda c: c["created_at"])
    completed.sort(key=lambda c: c["decided_at"] or c["updated_at"],
                   reverse=True)
    return {"active": active, "completed": completed}


def get_patient(patient_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM patients WHERE id = ?",
                           (patient_id,)).fetchone()
        if row is None:
            return None
        events = [{"ts": e["ts"], "kind": e["kind"], "message": e["message"],
                   **({"payload": json.loads(e["payload_json"])}
                      if e["payload_json"] else {})}
                  for e in conn.execute(
                      "SELECT * FROM events WHERE patient_id = ? ORDER BY id",
                      (patient_id,))]
    finally:
        conn.close()
    return {**_card(row), "record": json.loads(row["record_json"]),
            "result": _loads(row["result_json"]),
            "call": _loads(row["call_json"]), "events": events}


def get_evidence(evidence_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT payload_json FROM evidence WHERE id = ?",
                           (evidence_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else json.loads(row["payload_json"])


# --------------------------------------------------------------------------
# Writes


def _touch(conn: sqlite3.Connection, patient_id: str, ts: str) -> None:
    conn.execute("UPDATE patients SET updated_at = ? WHERE id = ?",
                 (ts, patient_id))


def _append_event(conn: sqlite3.Connection, patient_id: str, kind: str,
                  message: str, payload: dict | None = None,
                  ts: str | None = None) -> None:
    conn.execute(
        "INSERT INTO events (patient_id, ts, kind, message, payload_json) "
        "VALUES (?,?,?,?,?)",
        (patient_id, ts or now_iso(), kind, message, _dumps(payload)))


def append_event(patient_id: str, kind: str, message: str,
                 payload: dict | None = None) -> None:
    ts = now_iso()
    conn = _connect()
    try:
        _append_event(conn, patient_id, kind, message, payload, ts)
        _touch(conn, patient_id, ts)
        conn.commit()
    finally:
        conn.close()


def set_stage(patient_id: str, stage: str, message: str) -> None:
    ts = now_iso()
    conn = _connect()
    try:
        conn.execute("UPDATE patients SET stage = ?, updated_at = ? "
                     "WHERE id = ?", (stage, ts, patient_id))
        _append_event(conn, patient_id, "stage_change", message,
                      {"stage": stage}, ts)
        conn.commit()
    finally:
        conn.close()


def claim(patient_id: str, allowed: tuple[str, ...],
          steps: list[tuple[str, str]]) -> bool:
    """Walk the patient through `steps` only if it is still in `allowed`.

    One conditional UPDATE rather than a read followed by a write: two clicks
    on the same button would otherwise both pass the guard, run twice, and
    leave a duplicated timeline and an orphaned receipt. The intermediate
    stages are transient, so they are recorded in the same transaction as the
    stage that sticks.
    """
    ts = now_iso()
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE patients SET stage = ?, updated_at = ? WHERE id = ? "
            f"AND stage IN ({','.join('?' * len(allowed))})",
            (steps[-1][0], ts, patient_id, *allowed))
        if not cursor.rowcount:
            return False
        for stage, message in steps:
            _append_event(conn, patient_id, "stage_change", message,
                          {"stage": stage}, ts)
        conn.commit()
        return True
    finally:
        conn.close()


def not_submitted(patient_id: str, reasoning: str) -> None:
    """Close a case without ever sending it to the payer.

    The one home for the pre-submission negative outcome. Its wording is
    legally load-bearing, so both callers that reach it share this text rather
    than each carrying a copy that can drift.
    """
    write_decision(patient_id, {
        "outcome": "NOT_SUBMITTED", "source": "conduit",
        "reasoning": reasoning, "decided_at": now_iso()})
    set_stage(patient_id, "not_qualified", f"Not submitted. {reasoning}")


def write_result(patient_id: str, result: dict) -> None:
    ts = now_iso()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE patients SET result_json = ?, score = ?, band = ?, "
            "updated_at = ? WHERE id = ?",
            (json.dumps(result), result["score"], result["band"], ts,
             patient_id))
        conn.commit()
    finally:
        conn.close()


def write_decision(patient_id: str, decision: dict) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE patients SET decision_json = ?, decided_at = ?, "
            "updated_at = ? WHERE id = ?",
            (json.dumps(decision), decision["decided_at"], now_iso(),
             patient_id))
        conn.commit()
    finally:
        conn.close()


def write_appointment(patient_id: str, appointment: dict) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE patients SET appointment_json = ?, booked_at = ?, "
            "updated_at = ? WHERE id = ?",
            (json.dumps(appointment), appointment["booked_at"], now_iso(),
             patient_id))
        conn.commit()
    finally:
        conn.close()


def write_call(patient_id: str, call: dict | None) -> None:
    ts = now_iso()
    conn = _connect()
    try:
        conn.execute("UPDATE patients SET call_json = ?, updated_at = ? "
                     "WHERE id = ?", (_dumps(call), ts, patient_id))
        conn.commit()
    finally:
        conn.close()


def _insert_evidence(conn: sqlite3.Connection, patient_id: str,
                     payload: dict) -> str:
    evidence_id = f"ev-{uuid.uuid4().hex[:12]}"
    conn.execute("INSERT INTO evidence (id, patient_id, created_at, "
                 "payload_json) VALUES (?,?,?,?)",
                 (evidence_id, patient_id, payload["created_at"],
                  json.dumps(payload)))
    return evidence_id


def write_evidence(patient_id: str, payload: dict) -> str:
    conn = _connect()
    try:
        evidence_id = _insert_evidence(conn, patient_id, payload)
        conn.execute("UPDATE patients SET evidence_id = ? WHERE id = ?",
                     (evidence_id, patient_id))
        conn.commit()
        return evidence_id
    finally:
        conn.close()


def reset() -> None:
    global _EPOCH
    conn = _connect()
    try:
        # Plain execute, never executescript: these deletes and the reseed
        # share one deferred transaction that `_seed` commits, so a concurrent
        # reader never sees an empty caseload. executescript would commit each
        # statement on its own and expose exactly that gap.
        for table in ("events", "evidence", "patients"):
            conn.execute(f"DELETE FROM {table}")
        _seed(conn)
        # After the reseed commits, so a writer that reads the epoch mid-reset
        # still sees the old value and discards itself.
        _EPOCH += 1
    finally:
        conn.close()
