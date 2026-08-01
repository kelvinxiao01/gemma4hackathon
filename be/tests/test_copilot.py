"""Runs in any checkout: no Ollama, no network.

The tracker writes to a per-test sqlite file, so nothing here touches the
database the demo runs against.
"""

import json
import time
import re
import unicodedata
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilot import llm, orchestrate, qualify, tracker
from copilot.api import _search_health, router
from copilot.score import score_criteria

CARD_KEYS = {"id", "name", "payer", "payer_name", "drug", "brand_name",
             "plan_type", "stage", "score", "band", "decision", "appointment",
             "evidence_id", "created_at", "decided_at", "booked_at",
             "updated_at"}


def _c(kind, status, criterion="criterion"):
    return {"criterion": criterion, "kind": kind, "status": status,
            "evidence": "record line", "citation": {}}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "tracker.db")
    return tmp_path


def _fake_excerpts(patient):
    """Stand in for retrieval: one excerpt, one citation, no index needed."""
    doc = tracker.policy_doc(patient["payer"], patient["drug"],
                             patient["plan_type"])
    return "[1] policy excerpt", [{
        "payer_name": doc["payer_name"], "plan_type": doc["plan_type"],
        "ref": "p.2", "doc_url": doc["doc_url"], "quote": "policy excerpt"}]


def _fake_stream(patient):
    """A model reply in the real wire shape: reasoning, sentinel, then JSON.

    The statuses come from the same record the live model would read, so the
    two intake patients still diverge the way the demo needs.
    """
    criteria, summary = qualify._canned_criteria(patient)
    payload = {"criteria": [{"criterion": c["criterion"], "kind": c["kind"],
                             "status": c["status"], "evidence": c["evidence"],
                             "citation": 1} for c in criteria],
               "summary": summary}
    def stream(messages, **kwargs):
        for line in qualify._CANNED_REASONING:
            yield line + "\n"
        # Split across chunks so the sentinel has to be matched on a window.
        yield "===RES"
        yield "ULT===\n"
        yield json.dumps(payload)
    return stream


@pytest.fixture
def client(db, monkeypatch):
    # The payer answers on a real timer. Left armed, every submit test would
    # leave an eight second thread running and racing the assertions.
    monkeypatch.setattr(orchestrate, "start_payer_decision",
                        lambda *args: None)
    # The canned stream paces itself for a human watching. Tests pay that eight
    # times per qualification and learn nothing from it.
    monkeypatch.setattr(qualify, "CANNED_CADENCE", 0)
    monkeypatch.setattr(qualify, "_retrieve", _fake_excerpts)
    monkeypatch.setattr(
        qualify.llm, "stream_chat",
        lambda messages, **kw: _fake_stream(_stream_patient[0])(messages))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


# The fake stream needs the patient the request is for; `stream_chat` only
# receives messages, so the route records it here on the way past.
_stream_patient = [None]


@pytest.fixture(autouse=True)
def _capture_patient(monkeypatch):
    original = qualify.run

    def wrapped(patient, sink):
        _stream_patient[0] = patient
        return original(patient, sink)
    monkeypatch.setattr(qualify, "run", wrapped)


# --- the band rule ----------------------------------------------------------

def test_all_met_scores_one_hundred_and_qualifies():
    criteria = [_c("required", "MET")] * 4 + [_c("supporting", "MET")] * 4
    assert score_criteria(criteria) == (100, "QUALIFIES")


def test_one_required_contradiction_disqualifies_despite_high_points():
    """A contradicted requirement stops the case whatever the arithmetic says.

    Three required plus every supporting criterion still earns 80 points, well
    clear of the 60 floor, so only the explicit NOT_MET rule catches this.
    """
    criteria = ([_c("required", "MET")] * 3 + [_c("required", "NOT_MET")]
                + [_c("supporting", "MET")] * 4)
    score, band = score_criteria(criteria)
    assert (score, band) == (80, "NOT_QUALIFIED")


def test_one_required_unknown_routes_to_review():
    criteria = ([_c("required", "MET")] * 3 + [_c("required", "UNKNOWN")]
                + [_c("supporting", "MET")] * 4)
    assert score_criteria(criteria) == (80, "NEEDS_REVIEW")


def test_undocumented_and_contradicted_are_not_the_same_band():
    """The split that drives the product: UNKNOWN reviews, NOT_MET stops."""
    met = [_c("required", "MET")] * 3 + [_c("supporting", "MET")] * 4
    assert score_criteria(met + [_c("required", "UNKNOWN")])[1] == "NEEDS_REVIEW"
    assert score_criteria(met + [_c("required", "NOT_MET")])[1] == "NOT_QUALIFIED"


@pytest.mark.parametrize("n_required", [3, 5])
def test_normalisation_keeps_an_all_met_record_at_one_hundred(n_required):
    criteria = ([_c("required", "MET")] * n_required
                + [_c("supporting", "MET")] * 4)
    assert score_criteria(criteria) == (100, "QUALIFIES")


@pytest.mark.parametrize("n_required,expected", [(3, 73), (5, 84)])
def test_normalisation_splits_the_required_pool_evenly(n_required, expected):
    criteria = ([_c("required", "MET")] * (n_required - 1)
                + [_c("required", "UNKNOWN")]
                + [_c("supporting", "MET")] * 4)
    score, band = score_criteria(criteria)
    assert (score, band) == (expected, "NEEDS_REVIEW")


def test_a_policy_with_no_supporting_criteria_can_still_qualify():
    """The required pool absorbs the missing one, so 80 does not become the cap."""
    assert score_criteria([_c("required", "MET")] * 4) == (100, "QUALIFIES")


def test_an_empty_result_does_not_qualify():
    """Nothing checked routes to a human, never to an adverse determination.

    An empty extraction means the record was not read, which is undocumented
    rather than contradicted. Landing it on NOT_QUALIFIED would close the case
    as criteria not met with no human ever seeing it.
    """
    assert score_criteria([]) == (0, "NEEDS_REVIEW")


def test_an_entirely_undocumented_record_routes_to_review_not_refusal():
    """Undocumented is not contradicted, whatever the points say.

    Every requirement UNKNOWN scores below the floor, but closing the case on
    that alone would be an adverse determination no human ever saw.
    """
    undocumented = ([_c("required", "UNKNOWN")] * 4
                    + [_c("supporting", "MET")] * 4)
    score, band = score_criteria(undocumented)
    assert score < 60 and band == "NEEDS_REVIEW"

    # One requirement actually determined puts the floor back in play.
    partly = ([_c("required", "MET")] + [_c("required", "UNKNOWN")] * 3
              + [_c("supporting", "MET")] * 4)
    assert score_criteria(partly) == (40, "NOT_QUALIFIED")


def test_supporting_criteria_alone_cannot_qualify_a_patient():
    """No required criterion means nothing load-bearing was checked."""
    assert score_criteria([_c("supporting", "MET")] * 4)[1] == "NEEDS_REVIEW"


def test_the_kind_and_status_the_model_writes_are_read_case_insensitively():
    """A supporting criterion must not screen a patient out on letter case.

    The model writes both fields. Matched exactly, "Supporting" bins as
    required, and a supporting criterion the record contradicts would then
    produce NOT_SUBMITTED, which is reserved for required criteria.
    """
    record = ([_c("required", "MET")] * 4 + [_c("Supporting", "MET")] * 3
              + [_c("Supporting", "NOT_MET")])
    assert score_criteria(record) == (95, "QUALIFIES")
    assert score_criteria([_c("REQUIRED", "met")] * 4) == (100, "QUALIFIES")


def test_an_unrecognised_kind_is_treated_as_required():
    """Mis-binning a requirement into the cheap pool would let it pass unchecked."""
    criteria = [_c("required", "MET")] * 3 + [_c("mandatory", "NOT_MET")]
    assert score_criteria(criteria)[1] == "NOT_QUALIFIED"


# --- seeding ----------------------------------------------------------------

def test_seed_serves_three_active_and_eight_completed(client):
    body = client.get("/copilot/patients").json()
    assert (len(body["active"]), len(body["completed"])) == (3, 8)


def test_every_seeded_card_carries_the_frozen_key_set(client):
    body = client.get("/copilot/patients").json()
    for card in body["active"] + body["completed"]:
        assert set(card) == CARD_KEYS


def test_the_patient_detail_envelope_carries_the_frozen_key_set(client):
    detail = client.get("/copilot/patients/pt-ravi").json()
    assert set(detail) == CARD_KEYS | {"record", "result", "call", "events"}
    assert set(detail["record"]) == {
        "age", "weight_kg", "height_cm", "diagnosis", "treatment_history",
        "screening", "provider"}
    assert set(detail["result"]) == {"score", "band", "criteria", "summary"}


def test_every_receipt_and_citation_carries_the_frozen_key_set(client):
    body = client.get("/copilot/patients").json()
    for card in body["active"] + body["completed"]:
        if not card["evidence_id"]:
            continue
        receipt = client.get(f"/copilot/evidence/{card['evidence_id']}").json()
        assert set(receipt) == {
            "patient_id", "patient_name", "determination", "score", "band",
            "score_rule", "criteria", "doc_sha256", "doc_url", "model",
            "created_at", "disclaimer"}
        for criterion in receipt["criteria"]:
            assert set(criterion) == {"criterion", "kind", "status",
                                      "evidence", "citation"}
            assert set(criterion["citation"]) == {
                "payer_name", "plan_type", "ref", "doc_url", "quote"}


def test_a_source_without_pagination_cites_its_url(client):
    """Ana's policy is html, so `ref` falls back to the document URL."""
    ana = client.get("/copilot/patients/pt-ana").json()
    citation = ana["result"]["criteria"][0]["citation"]
    assert citation["ref"] == citation["doc_url"]
    assert citation["doc_url"].startswith("http")

    elena = client.get("/copilot/patients/pt-elena").json()
    assert elena["result"]["criteria"][0]["citation"]["ref"] == "p.2"


def test_every_booked_appointment_carries_the_frozen_key_set(client):
    body = client.get("/copilot/patients").json()
    booked = [c for c in body["completed"] if c["appointment"]]
    assert len(booked) == 3
    for card in booked:
        assert set(card["appointment"]) == {"center_name", "address", "phone",
                                            "starts_at", "booked_at"}


def test_every_timestamp_carries_a_timezone(client):
    """The frontend parses these; a naive instant renders in the wrong zone."""
    body = client.get("/copilot/patients").json()
    for card in body["active"] + body["completed"]:
        detail = client.get(f"/copilot/patients/{card['id']}").json()
        stamps = [card[k] for k in ("created_at", "decided_at", "booked_at",
                                    "updated_at")]
        stamps += [e["ts"] for e in detail["events"]]
        if card["decision"]:
            stamps.append(card["decision"]["decided_at"])
        if card["appointment"]:
            stamps += [card["appointment"][k]
                       for k in ("starts_at", "booked_at")]
        for stamp in [s for s in stamps if s]:
            assert datetime.fromisoformat(stamp).tzinfo is not None, stamp


def test_the_seeder_applies_the_fixture_offsets(client):
    """Offsets, not absolute dates, so the caseload reads as live work."""
    body = client.get("/copilot/patients").json()
    created = {c["id"]: c["created_at"] for c in
               body["active"] + body["completed"]}
    assert created["pt-owen"] < created["pt-dana"]


def test_completed_cards_lead_with_the_newest_determination(client):
    """A card that finishes on stage has to arrive at the top of the list."""
    completed = client.get("/copilot/patients").json()["completed"]
    stamps = [c["decided_at"] or c["updated_at"] for c in completed]
    assert stamps == sorted(stamps, reverse=True)


# Escapes, not the glyphs themselves, so tracked files stay pure ASCII. These
# are exactly the characters the source PDFs carry and the fixtures do not.
_GLYPHS = [("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'),
           ("\u2265", ">="), ("\u2264", "<="), ("\u2022", " "),
           ("\u00a0", " "), ("\u2014", " "), ("\u2013", " ")]


def _normalise(text):
    """Fold the glyphs the source PDFs use into the ASCII the fixtures carry."""
    text = unicodedata.normalize("NFKD", text)
    for glyph, plain in _GLYPHS:
        text = text.replace(glyph, plain)
    return re.sub(r"\s+", " ", text).strip().lower()


def test_every_seeded_quote_appears_on_the_page_it_cites(client):
    """A receipt prints a quote beside the hash of the document it came from.

    Nothing else checks the quote against that document, so a row cloned from
    another patient can certify text the cited policy does not contain.
    """
    body = client.get("/copilot/patients").json()
    for card in body["active"] + body["completed"]:
        if not card["evidence_id"]:
            continue
        receipt = client.get(f"/copilot/evidence/{card['evidence_id']}").json()
        corpus = json.loads(
            (tracker.CORPUS / f"{card['payer']}_{card['drug']}_"
                              f"{card['plan_type']}.json").read_text())
        pages = {p["page"]: _normalise(p["text"]) for p in corpus["pages"]}
        whole = _normalise(" ".join(p["text"] for p in corpus["pages"]))
        for criterion in receipt["criteria"]:
            citation = criterion["citation"]
            ref = citation["ref"]
            haystack = (pages[int(ref.removeprefix("p."))]
                        if ref.startswith("p.") else whole)
            assert _normalise(citation["quote"]) in haystack, (
                f"{card['id']} cites {ref}: {citation['quote'][:70]}")


def test_no_conduit_sourced_decision_mentions_a_denial(client):
    """Label law: the word may only ever appear with source payer."""
    body = client.get("/copilot/patients").json()
    for card in body["active"] + body["completed"]:
        decision = card["decision"]
        if decision and decision["source"] == "conduit":
            assert "deni" not in decision["reasoning"].lower()


def test_every_seeded_receipt_resolves_with_the_document_hash(client):
    body = client.get("/copilot/patients").json()
    seeded = [c for c in body["active"] + body["completed"] if c["evidence_id"]]
    assert len(seeded) == 9
    for card in seeded:
        receipt = client.get(f"/copilot/evidence/{card['evidence_id']}").json()
        doc = tracker.policy_doc(card["payer"], card["drug"], card["plan_type"])
        assert receipt["doc_sha256"] == doc["source_sha256"]
        assert receipt["model"] == "seeded"
        assert receipt["score_rule"].startswith("Twenty points")
        assert receipt["disclaimer"] == tracker.DISCLAIMER


def test_unknown_evidence_id_is_a_404(client):
    assert client.get("/copilot/evidence/ev-nope").status_code == 404


def test_unknown_patient_id_is_a_404(client):
    assert client.get("/copilot/patients/pt-nobody").status_code == 404


def test_reset_restores_the_caseload_after_a_qualification(client):
    client.post("/copilot/patients/pt-dana/qualify")
    assert len(client.get("/copilot/patients").json()["active"]) == 2

    assert client.post("/copilot/reset").json() == {"ok": True}
    body = client.get("/copilot/patients").json()
    assert (len(body["active"]), len(body["completed"])) == (3, 8)
    assert body["active"][0]["stage"] == "intake"


# --- transitions ------------------------------------------------------------

def test_a_contradicted_requirement_completes_the_patient_without_submission(
        client):
    stream = client.post("/copilot/patients/pt-dana/qualify")
    assert stream.status_code == 200
    assert "===RESULT===" not in stream.text

    patient = client.get("/copilot/patients/pt-dana").json()
    assert patient["stage"] == "not_qualified"
    assert patient["band"] == "NOT_QUALIFIED"
    assert patient["decision"]["outcome"] == "NOT_SUBMITTED"
    assert patient["decision"]["source"] == "conduit"
    assert "14-week" in patient["decision"]["reasoning"]
    assert patient["evidence_id"]

    kinds = [e["kind"] for e in patient["events"]]
    assert kinds.index("qualify_started") < kinds.index("qualify_done")
    assert kinds[-1] == "qualify_done"
    # The stage lands before the result is announced, so a dashboard polling
    # between the two never shows a finished determination on an open case.
    assert patient["events"][-2]["payload"]["stage"] == "not_qualified"


def test_a_clean_record_routes_to_review_and_scores_higher(client):
    client.post("/copilot/patients/pt-marcus/qualify")
    marcus = client.get("/copilot/patients/pt-marcus").json()
    assert marcus["stage"] == "review"
    assert marcus["band"] == "QUALIFIES"
    assert marcus["score"] >= 85

    client.post("/copilot/patients/pt-dana/qualify")
    dana = client.get("/copilot/patients/pt-dana").json()
    # The single most important property of the whole flow: two different
    # records must not produce the same determination.
    assert dana["score"] != marcus["score"]


def test_qualifying_twice_is_a_409(client):
    client.post("/copilot/patients/pt-marcus/qualify")
    assert client.post(
        "/copilot/patients/pt-marcus/qualify").status_code == 409


def test_qualifying_a_patient_past_intake_is_a_409(client):
    assert client.post("/copilot/patients/pt-ravi/qualify").status_code == 409


def test_submit_before_review_is_a_409(client):
    r = client.post("/copilot/patients/pt-dana/submit", json={"action": "submit"})
    assert r.status_code == 409


def test_submit_names_the_payer_in_the_story_event(client):
    client.post("/copilot/patients/pt-marcus/qualify")
    r = client.post("/copilot/patients/pt-marcus/submit",
                    json={"action": "submit"})
    assert r.json() == {"ok": True, "stage": "submitted"}

    marcus = client.get("/copilot/patients/pt-marcus").json()
    assert marcus["stage"] == "submitted"
    submitted = [e for e in marcus["events"] if e["kind"] == "submitted"]
    assert submitted[0]["message"] == (
        "Clinical criteria passed. Prior authorization request submitted to "
        "UnitedHealthcare.")
    # Still in progress: the payer has not answered yet.
    active = client.get("/copilot/patients").json()["active"]
    assert "pt-marcus" in [c["id"] for c in active]


def test_do_not_submit_completes_without_a_payer_decision(client):
    client.post("/copilot/patients/pt-marcus/qualify")
    r = client.post("/copilot/patients/pt-marcus/submit",
                    json={"action": "do_not_submit"})
    assert r.json() == {"ok": True, "stage": "not_qualified"}

    marcus = client.get("/copilot/patients/pt-marcus").json()
    assert marcus["decision"]["outcome"] == "NOT_SUBMITTED"
    assert marcus["decision"]["source"] == "conduit"


def test_submit_rejects_an_action_it_does_not_define(client):
    client.post("/copilot/patients/pt-marcus/qualify")
    r = client.post("/copilot/patients/pt-marcus/submit",
                    json={"action": "withdraw"})
    assert r.status_code == 422


def test_schedule_requires_an_approved_patient_at_scheduling(client):
    assert client.post(
        "/copilot/patients/pt-dana/schedule").status_code == 409
    assert client.post(
        "/copilot/patients/pt-ravi/schedule").status_code == 200


def test_the_live_path_splits_the_sentinel_and_resolves_citations(
        client, monkeypatch):
    """The model's reply is reasoning, then a sentinel, then JSON.

    Neither the sentinel nor the JSON may reach the client, and the excerpt
    number the model writes has to become a real citation.
    """
    monkeypatch.setattr(qualify, "LIVE", True)
    body = client.post("/copilot/patients/pt-dana/qualify").text
    assert "===RESULT===" not in body
    assert "{" not in body
    assert "Criterion 1 of 4" in body

    dana = client.get("/copilot/patients/pt-dana").json()
    assert dana["result"]["criteria"][0]["citation"]["ref"] == "p.2"
    receipt = client.get(
        f"/copilot/evidence/{dana['evidence_id']}").json()
    assert receipt["model"] == qualify.llm.MODEL


def test_an_out_of_range_citation_falls_back_to_the_first_excerpt():
    citations = [{"ref": "p.2"}, {"ref": "p.3"}]
    assert qualify.context.resolve(9, citations)["ref"] == "p.2"
    assert qualify.context.resolve("not a number", citations)["ref"] == "p.2"
    assert qualify.context.resolve(2, citations)["ref"] == "p.3"


def test_a_reply_with_no_json_after_the_sentinel_does_not_persist(
        client, monkeypatch):
    """A malformed reply returns the case to the queue, never a determination."""
    monkeypatch.setattr(qualify, "LIVE", True)
    monkeypatch.setattr(qualify.llm, "stream_chat",
                        lambda messages, **kw: iter(["thinking\n",
                                                     "===RESULT===\n",
                                                     "not json at all"]))
    assert client.post("/copilot/patients/pt-dana/qualify").status_code == 200
    dana = client.get("/copilot/patients/pt-dana").json()
    assert dana["stage"] == "intake"
    assert dana["result"] is None


def test_scheduling_books_the_nearest_center_that_offers_the_drug(
        client, monkeypatch):
    # Run the call steps inline instead of on timers, so the assertions do not
    # race a thread pool.
    monkeypatch.setattr(orchestrate, "_later",
                        lambda delay, fn, *args: fn(*args))
    assert client.post(
        "/copilot/patients/pt-ravi/schedule").status_code == 200

    ravi = client.get("/copilot/patients/pt-ravi").json()
    assert ravi["stage"] == "booked"
    assert ravi["appointment"]["center_name"] == "Hudson Infusion Center"
    # 07:15 is outside business hours, so the picker takes the next slot.
    assert ravi["appointment"]["starts_at"].endswith("T09:30:00-04:00")

    kinds = [e["kind"] for e in ravi["events"]]
    for expected in ("center_search", "slot_identified", "call_created",
                     "call_dialing", "call_completed", "booked"):
        assert expected in kinds

    search = next(e for e in ravi["events"] if e["kind"] == "center_search")
    offered = [c for c in search["payload"]["centers"] if c["offers_drug"]]
    assert len(offered) == 2 and len(search["payload"]["centers"]) == 3


def test_submit_hands_the_case_to_the_payer(client):
    """The payer answers on a timer; here it is invoked directly."""
    client.post("/copilot/patients/pt-marcus/qualify")
    client.post("/copilot/patients/pt-marcus/submit", json={"action": "submit"})
    orchestrate._payer_decided("pt-marcus", "UnitedHealthcare",
                               tracker.epoch())

    marcus = client.get("/copilot/patients/pt-marcus").json()
    assert marcus["stage"] == "scheduling"
    assert marcus["decision"]["outcome"] == "APPROVED"
    assert marcus["decision"]["source"] == "payer"


def test_an_interrupted_qualification_returns_the_case_to_the_queue(
        client, monkeypatch):
    """A determination that never arrived must not strand the patient.

    The stage moves to `qualifying` before the first chunk, and the qualify
    guard does not admit `qualifying`, so a run that produces no criteria has
    to put the case back or it can never be retried.
    """
    monkeypatch.setattr(qualify, "_canned_criteria", lambda patient: ([], ""))
    assert client.post("/copilot/patients/pt-dana/qualify").status_code == 200

    dana = client.get("/copilot/patients/pt-dana").json()
    assert dana["stage"] == "intake"
    assert dana["decision"] is None
    assert dana["band"] is None
    # The whole point: the specialist can run it again.
    assert client.post("/copilot/patients/pt-dana/qualify").status_code == 200


def test_a_determination_is_dropped_if_the_caseload_was_reset_under_it(client):
    """A reset reseeds the same ids, so a late write would hit the new row."""
    dana = tracker.get_patient("pt-dana")
    stale = tracker.epoch()
    tracker.reset()

    qualify.finalize(dana, stale)

    after = tracker.get_patient("pt-dana")
    assert after["stage"] == "intake"
    assert after["result"] is None
    assert after["decision"] is None


def test_a_second_qualification_cannot_claim_a_patient_already_running(client):
    """The guard is one conditional update, so a double-click loses the race."""
    steps = [("policy_matched", "matched"), ("qualifying", "reading")]
    assert tracker.claim("pt-dana", ("intake", "policy_matched"), steps)
    assert not tracker.claim("pt-dana", ("intake", "policy_matched"), steps)

    events = tracker.get_patient("pt-dana")["events"]
    assert [e["message"] for e in events].count("reading") == 1


def test_the_stage_walk_is_recorded_with_the_stage_it_moved_to(client):
    """A timeline keyed on payload.stage has to work on seeded cards too."""
    client.post("/copilot/patients/pt-dana/qualify")
    dana = client.get("/copilot/patients/pt-dana").json()
    walked = [e["payload"]["stage"] for e in dana["events"]
              if e["kind"] == "stage_change"]
    assert walked == ["intake", "policy_matched", "qualifying",
                      "not_qualified"]

    for seeded in ("pt-elena", "pt-victor", "pt-ana"):
        patient = client.get(f"/copilot/patients/{seeded}").json()
        changes = [e for e in patient["events"] if e["kind"] == "stage_change"]
        assert all("stage" in e["payload"] for e in changes)
        # The timeline has to end where the card says the case rests.
        assert changes[-1]["payload"]["stage"] == patient["stage"]


def test_the_canned_stream_actually_streams_the_reasoning(client):
    body = client.post("/copilot/patients/pt-marcus/qualify").text
    assert "Criterion 1 of 4" in body
    assert body.count("\n") >= 8


def test_do_not_submit_names_the_criterion_the_specialist_declined_on(client):
    client.post("/copilot/patients/pt-marcus/qualify")
    client.post("/copilot/patients/pt-marcus/submit",
                json={"action": "do_not_submit"})
    reasoning = client.get(
        "/copilot/patients/pt-marcus").json()["decision"]["reasoning"]
    assert reasoning == "Specialist elected not to submit this request."


def test_schedule_refuses_a_patient_at_scheduling_without_an_approval(client):
    """Both halves of the guard, not just the stage."""
    tracker.write_decision("pt-ravi", {
        "outcome": "PAYER_DENIED", "source": "payer", "reasoning": "no",
        "decided_at": tracker.now_iso()})
    assert client.post(
        "/copilot/patients/pt-ravi/schedule").status_code == 409


def test_health_reports_the_index_without_creating_it(tmp_path, monkeypatch):
    """The probe must never be the thing that makes an empty index."""
    missing = tmp_path / "absent.db"
    monkeypatch.setattr("copilot.api.INDEX_DB_PATH", missing)
    assert _search_health() == "unavailable"
    assert not missing.exists()


def test_a_warm_model_that_hits_the_token_ceiling_still_reports_ok(monkeypatch):
    """A short num_predict trips done_reason, which does not mean unreachable."""
    def truncated(*args, **kwargs):
        raise RuntimeError("[gemma error] incomplete generation: length")
    monkeypatch.setattr(llm, "chat", truncated)
    assert llm.ping() == "ok"


def test_an_unreachable_model_is_reported_as_unreachable(monkeypatch):
    def refused(*args, **kwargs):
        raise OSError("connection refused")
    monkeypatch.setattr(llm, "chat", refused)
    assert llm.ping() == "unreachable"


def test_the_readiness_probe_does_not_wait_out_a_generation(monkeypatch):
    """Ollama serialises per model, so an unbounded probe queues behind one."""
    seen = {}

    def record(messages, schema=None, num_predict=1024, timeout=None):
        seen["timeout"] = timeout
        return "ready"
    monkeypatch.setattr(llm, "chat", record)
    llm.ping()
    assert seen["timeout"] == llm.PING_TIMEOUT
    assert llm.PING_TIMEOUT < llm.TIMEOUT


def test_posted_events_land_on_the_timeline(client):
    r = client.post("/copilot/patients/pt-ravi/events",
                    json={"kind": "note", "message": "Called the clinic.",
                          "payload": {"by": "specialist"}})
    assert r.json() == {"ok": True}
    last = client.get("/copilot/patients/pt-ravi").json()["events"][-1]
    assert last["kind"] == "note"
    assert last["message"] == "Called the clinic."
    assert last["payload"] == {"by": "specialist"}
    assert last["ts"]
