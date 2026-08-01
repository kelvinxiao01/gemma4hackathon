"""Run one patient record against the payer's published criteria.

Only the criterion statuses come from the model. The score, the band, and
every stage transition are computed here, so a determination is reproducible
and no coverage judgement is carried by generated text.
"""

import json
import time
from collections.abc import Iterator

from docsearch.store import search

from . import context, llm, tracker
from .score import score_criteria

SENTINEL = "===RESULT==="

# Qualification runs against the deterministic path by default. Gemma reads the
# policy and cites it correctly, but it does not reliably surface the
# step-therapy criterion a contradicted record turns on, and a determination
# that silently drops the criterion it was meant to catch is worse than one
# that is reproducible. Set LIVE to True to qualify against the model instead;
# the retrieval, the prompt, the sentinel protocol and everything downstream of
# `persist` are identical on both paths.
LIVE = False

# Two queries, not one: the measured specialist question ranks the initial
# approval criteria, and the flat phrase pulls in the clauses it misses. The
# 14-week biosimilar criterion that Dana's story turns on lands at rank 5.
TOP_K_SPECIALIST = 6
TOP_K_BROAD = 4
MAX_EXCERPTS = 8

SYSTEM_PROMPT = """\
You check one patient record against the payer's own published coverage \
criteria. You do not decide coverage and you do not score anything.

Use ONLY the numbered excerpts. Never state a requirement they do not contain.

Tag each criterion `required` if the policy makes it a condition of approval, \
`supporting` if it is a dosing, administration or limitation detail.

Status has exactly three values and the difference matters more than anything \
else you do:
  MET       the record satisfies the requirement.
  NOT_MET   the record CONTRADICTS the requirement, for example a 9 week trial \
where the policy sets a 14 week floor. Only use this when the record states \
something incompatible with the requirement.
  UNKNOWN   the requirement is simply not documented in the record. If you are \
unsure, this is the answer.

Write one short reasoning line per criterion, plain prose, no markdown.
Then write the line ===RESULT=== on its own.
Then write ONLY this JSON and nothing after it:
{"criteria":[{"criterion":"...","kind":"required|supporting",\
"status":"MET|NOT_MET|UNKNOWN","evidence":"what the record says",\
"citation":<excerpt number>}],"summary":"one or two sentences"}"""


def _retrieve(patient: dict) -> tuple[str, list[dict]]:
    diagnosis = patient["record"]["diagnosis"]
    drug, payer = patient["drug"], patient["payer"]
    hits = search(f"What must be documented for initial approval of {drug} "
                  f"in {diagnosis}?", payer=payer, drug=drug,
                  top_k=TOP_K_SPECIALIST)
    hits += search("initial approval criteria", payer=payer, drug=drug,
                   top_k=TOP_K_BROAD)

    seen, unique = set(), []
    for hit in hits:
        key = (hit.doc_url, hit.page_start, hit.text[:80])
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    return context.build(unique[:MAX_EXCERPTS])


def _messages(patient: dict, excerpts: str) -> list[dict]:
    # Excerpts first, question last: the long shared prefix stays stable across
    # patients on the same policy, which is what the prompt cache rewards.
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"POLICY EXCERPTS:\n{excerpts}\n\n"
            f"PATIENT RECORD:\n{json.dumps(patient['record'], indent=1)}\n\n"
            f"Check this record against the excerpts for {patient['drug']}."},
    ]


def _parse(buffered: str, citations: list[dict]) -> tuple[list[dict], str]:
    """Pull the criteria out of the post-sentinel JSON."""
    start = buffered.find("{")
    end = buffered.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object after the sentinel")
    payload = json.loads(buffered[start:end + 1])

    criteria = []
    for row in payload.get("criteria", []):
        criteria.append({
            "criterion": row.get("criterion", ""),
            "kind": row.get("kind", "required"),
            "status": row.get("status", "UNKNOWN"),
            "evidence": row.get("evidence", ""),
            "citation": context.resolve(row.get("citation"), citations),
        })
    return criteria, payload.get("summary", "")

# ponytail: canned reasoning and canned statuses, real transitions. A live
# Gemma call drops in behind the same generator signature, replacing
# `_canned_criteria` and the reasoning lines; everything from `persist` down is
# final either way.
CANNED_CADENCE = 0.1

_CANNED_REASONING = [
    "Retrieving UnitedHealthcare commercial policy for ustekinumab.",
    "Eight policy excerpts matched. Reading initial-therapy criteria.",
    "Criterion 1 of 4: diagnosis of moderately to severely active Crohn's disease.",
    "Criterion 2 of 4: failure of a conventional therapy at maximally indicated doses.",
    "Criterion 3 of 4: trial of at least 14 weeks of a preferred ustekinumab product.",
    "Criterion 4 of 4: prescribed by or in consultation with a gastroenterologist.",
    "Checking supporting criteria: induction route and concurrent therapy.",
    "Compiling the determination.",
]


def _canned_criteria(patient: dict) -> tuple[list[dict], str]:
    """Statuses for the two intake patients, keyed off the record they carry."""
    doc = tracker.policy_doc(patient["payer"], patient["drug"],
                             patient["plan_type"])
    weeks = 16 if "16 weeks" in " ".join(
        patient["record"]["treatment_history"]) else 9
    step_met = weeks >= 14
    rows = [
        ("Diagnosis of moderately to severely active Crohn's disease",
         "required", "MET",
         "Crohn's disease documented in the diagnosis field of the request.",
         "p.2", "Diagnosis of moderately to severely active Crohn's disease; and"),
        ("Failure of a conventional therapy at up to maximally indicated doses",
         "required", "MET",
         "Azathioprine documented at maximally indicated dose with inadequate "
         "response.",
         "p.2", "History of failure to one of the following conventional "
         "therapies at up to maximally indicated doses, unless contraindicated "
         "or clinically significant adverse effects are experienced:"),
        ("Trial of at least 14 weeks of a preferred ustekinumab product with "
         "minimal clinical response",
         "required", "MET" if step_met else "NOT_MET",
         f"Wezlana (ustekinumab-auub) course ran {weeks} weeks"
         + (" with minimal clinical response and residual disease activity."
            if step_met else
            ", short of the 14-week minimum the policy requires."),
         "p.2", "Documentation of a trial of at least 14 weeks of Starjemza, "
         "Steqeyma, Wezlana, or Yesintek"),
        ("Prescribed by or in consultation with a gastroenterologist",
         "required", "MET",
         f"Prescribed by {patient['record']['provider']['name']}, "
         f"{patient['record']['provider']['specialty']}.",
         "p.3", "Prescribed by or in consultation with a gastroenterologist; and"),
        ("Administered as a single intravenous induction dose",
         "supporting", "MET",
         "Order specifies a single weight-based intravenous induction dose.",
         "p.2", "Ustekinumab is to be administered as a single intravenous "
         "induction dose; and"),
        ("Induction dosing follows the FDA approved labeled dosing",
         "supporting", "MET",
         "Weight-based induction dosing recorded on the request.",
         "p.2", "Ustekinumab induction dosing is in accordance with the United "
         "States Food and Drug Administration approved labeled dosing for "
         "Crohn's disease; and"),
        ("Not receiving ustekinumab with another systemic targeted "
         "immunomodulator",
         "supporting", "MET",
         "No concurrent targeted immunomodulator recorded.",
         "p.2", "Patient is not receiving ustekinumab in combination with a "
         "systemic targeted immunomodulator [e.g., adalimumab, Cimzia "
         "(certolizumab), Entyvio (vedolizumab), Omvoh (mirikizumab-mrkz), "
         "Rinvoq (upadacitinib), Skyrizi (risankizumab), Tremfya "
         "(guselkumab)] for treatment of the same indication; and"),
        ("Authorization limited to one induction dose",
         "supporting", "MET",
         "Request covers a single induction dose.",
         "p.2", "Authorization will be for one induction dose"),
    ]
    criteria = [{"criterion": c, "kind": k, "status": s, "evidence": e,
                 "citation": {"payer_name": doc["payer_name"],
                              "plan_type": doc["plan_type"], "ref": ref,
                              "doc_url": doc["doc_url"], "quote": quote}}
                for c, k, s, e, ref, quote in rows]
    summary = (
        "All four required criteria are met and the record documents a "
        f"{weeks}-week preferred-product trial, which clears the "
        "step-therapy requirement." if step_met else
        "Three of four required criteria are met. The record documents a "
        f"{weeks}-week Wezlana course against the policy minimum of 14 weeks, "
        "so the preferred-product step is contradicted by the record.")
    return criteria, summary


def failed_criterion(criteria: list[dict]) -> dict | None:
    """The first required criterion the record contradicts, if any."""
    return next((c for c in criteria if c.get("kind") != "supporting"
                 and c.get("status") == "NOT_MET"), None)


def persist(patient: dict, criteria: list[dict], summary: str,
            model: str) -> dict:
    """Score, store the receipt, and move the patient to its next stage."""
    patient_id = patient["id"]
    score, band = score_criteria(criteria)
    result = {"score": score, "band": band, "criteria": criteria,
              "summary": summary}
    tracker.write_result(patient_id, result)

    doc = tracker.policy_doc(patient["payer"], patient["drug"],
                             patient["plan_type"])
    tracker.write_evidence(patient_id, tracker.receipt(
        patient_id, patient["name"], result, doc, model, tracker.now_iso()))

    if band == "NOT_QUALIFIED":
        failed = failed_criterion(criteria)
        reasoning = (f"{failed['criterion']}. {failed['evidence']}" if failed
                     else "Score below the threshold for submission.")
        tracker.not_submitted(patient_id, reasoning)
    else:
        tracker.set_stage(patient_id, "review",
                          "Qualification complete. Awaiting specialist review.")

    tracker.append_event(patient_id, "qualify_done", summary,
                         {"score": score, "band": band})
    return result


def run(patient: dict, sink: dict) -> Iterator[str]:
    """Stream the reasoning, leaving the determination in `sink`.

    `finalize` writes it afterwards as a background task. The determination has
    to survive the client hanging up, and a generator does not.
    """
    tracker.append_event(patient["id"], "qualify_started",
                         f"Qualifying against {patient['payer_name']} "
                         f"{patient['plan_type']} {patient['drug']} criteria.")

    if not LIVE:
        for line in _CANNED_REASONING:
            yield line + "\n"
            time.sleep(CANNED_CADENCE)
        sink["criteria"], sink["summary"] = _canned_criteria(patient)
        sink["model"] = "canned"
        return

    excerpts, citations = _retrieve(patient)
    if not citations:
        # No policy text means no grounded answer. Say so rather than let the
        # model invent criteria from its own memory of the drug.
        yield ("No coverage criteria for this drug and payer are present in "
               "the policy index.\n")
        return

    buffered, past_sentinel = [], False
    for piece in llm.stream_chat(_messages(patient, excerpts)):
        if past_sentinel:
            buffered.append(piece)
            continue
        # The sentinel can straddle two chunks, so match against the tail of
        # what has arrived rather than the chunk alone.
        window = "".join(buffered[-4:]) + piece
        if SENTINEL in window:
            past_sentinel = True
            buffered = [window.split(SENTINEL, 1)[1]]
            continue
        buffered.append(piece)
        yield piece

    try:
        sink["criteria"], sink["summary"] = _parse("".join(buffered),
                                                  citations)
        sink["model"] = llm.MODEL
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[gemma error] qualify parse: {type(exc).__name__}: {exc}")


def finalize(patient: dict, epoch: int, sink: dict | None = None) -> None:
    """Write the determination once the response has been delivered.

    This runs as a background task rather than at the tail of the generator.
    A client that disconnects mid-stream leaves the generator suspended at a
    yield until the cyclic garbage collector reaches it, so a tail there fires
    at an unpredictable time or not at all, and the patient sits at `qualifying`
    where the qualify guard will not readmit it.
    """
    patient_id = patient["id"]
    if epoch != tracker.epoch():
        # The caseload was wiped and reseeded while this ran. The patient this
        # determination describes no longer exists.
        return

    criteria = (sink or {}).get("criteria") or []
    if not criteria:
        # No determination to record. Undocumented is not the same as
        # contradicted, so the case goes back in the queue for a human to run
        # again rather than being closed as criteria not met.
        tracker.set_stage(patient_id, "intake",
                          "Qualification did not complete. Case returned to "
                          "the queue.")
        return
    persist(patient, criteria, sink.get("summary", ""),
            sink.get("model", "canned"))
