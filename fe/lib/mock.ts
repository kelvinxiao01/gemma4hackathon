// Mock caseload. The demo has to be rehearsable with no backend running, so
// every endpoint the UI calls has a mock twin here with the same shapes and the
// same timings. State lives in this module: in-app navigation keeps a run
// going, a hard reload starts over.
//
// Quotes are copied from the corpus documents the backend indexes, and the
// document hashes are those files' source_sha256. Patients, providers, and
// infusion centers are fictional; the NPIs fail the CMS check digit on purpose.

import {
  type Appointment,
  type Band,
  type CallInfo,
  type Card,
  type Center,
  type Citation,
  type Criterion,
  type Decision,
  type Event,
  type Evidence,
  type Health,
  type PatientDetail,
  type QualifyResult,
  type Stage,
  DISCLAIMER,
  SCORE_RULE,
  TERMINAL_STAGES,
} from "./types";

const START = Date.now();

/** ISO timestamp `minutes` before this module loaded. */
function ago(minutes: number): string {
  return new Date(START - minutes * 60_000).toISOString();
}

function now(): string {
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// Policy documents and criteria templates
// ---------------------------------------------------------------------------

const UHC_DOC = {
  payer: "uhc",
  payer_name: "UnitedHealthcare",
  plan_type: "commercial",
  doc_url:
    "https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/ustekinumab.pdf",
  sha256: "ac86f2920c18765638e5ff5d0a2fe3d1d82108ca18a95da00ecbadb90e83e515",
};

const ANTHEM_DOC = {
  payer: "anthem",
  payer_name: "Anthem",
  plan_type: "commercial",
  doc_url:
    "https://www.anthem.com/content/dam/digital/docs/pharmacy-information/clinical-criteria/ustekinumab.pdf",
  sha256: "d196b5715c495a3f94bb988d9db343a22d0240850673225c211576c5e4f4a778",
};

const DOCS = { uhc: UHC_DOC, anthem: ANTHEM_DOC };

/** The payer fields a Card carries, without the document metadata. */
function payerOf(doc: typeof UHC_DOC) {
  return {
    payer: doc.payer,
    payer_name: doc.payer_name,
    plan_type: doc.plan_type,
  };
}

type Slot = { criterion: string; kind: Criterion["kind"]; citation: Citation };
type Fill = { status: Criterion["status"]; evidence: string };
type Eight<T> = [T, T, T, T, T, T, T, T];

function cite(doc: typeof UHC_DOC, ref: string, quote: string): Citation {
  return {
    payer_name: doc.payer_name,
    plan_type: doc.plan_type,
    ref,
    doc_url: doc.doc_url,
    quote,
  };
}

const UHC_SLOTS: Eight<Slot> = [
  {
    criterion: "Diagnosis of moderately to severely active Crohn's disease",
    kind: "required",
    citation: cite(
      UHC_DOC,
      "p.2",
      "Ustekinumab is medically necessary for the treatment of Crohn's disease when all of the following criteria are met: Diagnosis of moderately to severely active Crohn's disease",
    ),
  },
  {
    criterion:
      "Documented failure of a conventional therapy (corticosteroids, 6-mercaptopurine, azathioprine, or methotrexate)",
    kind: "required",
    citation: cite(
      UHC_DOC,
      "p.2",
      "History of failure to one of the following conventional therapies at up to maximally indicated doses, unless contraindicated or clinically significant adverse effects are experienced: Corticosteroids (e.g., prednisone, methylprednisolone, budesonide); 6-mercaptopurine (Purinethol)",
    ),
  },
  {
    criterion:
      "Documented trial of at least 14 weeks of a preferred biosimilar with minimal clinical response",
    kind: "required",
    citation: cite(
      UHC_DOC,
      "p.2",
      "Documentation of a trial of at least 14 weeks of Starjemza, Steqeyma, Wezlana, or Yesintek resulting in minimal clinical response to therapy and residual disease activity",
    ),
  },
  {
    criterion:
      "Prescribed by or in consultation with a gastroenterologist",
    kind: "required",
    citation: cite(
      UHC_DOC,
      "p.3",
      "Prescribed by or in consultation with a gastroenterologist; and Authorization will be for one induction dose",
    ),
  },
  {
    criterion: "Administered as a single intravenous induction dose",
    kind: "supporting",
    citation: cite(
      UHC_DOC,
      "p.2",
      "Ustekinumab is to be administered as a single intravenous induction dose",
    ),
  },
  {
    criterion: "Induction dosing follows the FDA approved labeled dosing",
    kind: "supporting",
    citation: cite(
      UHC_DOC,
      "p.2",
      "Ustekinumab induction dosing is in accordance with the United States Food and Drug Administration approved labeled dosing for Crohn's disease",
    ),
  },
  {
    criterion:
      "Not given with a systemic targeted immunomodulator for the same indication",
    kind: "supporting",
    citation: cite(
      UHC_DOC,
      "p.2",
      "Patient is not receiving ustekinumab in combination with a systemic targeted immunomodulator [e.g., adalimumab, Cimzia (certolizumab), Entyvio (vedolizumab), Rinvoq (upadacitinib), Skyrizi (risankizumab), Tremfya (guselkumab)] for treatment of the same indication",
    ),
  },
  {
    criterion: "Request is for one induction dose",
    kind: "supporting",
    citation: cite(UHC_DOC, "p.2", "Authorization will be for one induction dose"),
  },
];

const ANTHEM_SLOTS: Eight<Slot> = [
  {
    criterion:
      "Individual is 2 years of age or older with moderate to severe Crohn's disease",
    kind: "required",
    citation: cite(
      ANTHEM_DOC,
      "p.2",
      "Crohn's disease (CD) when the following criterion is met: Individual is 2 years of age or older with moderate to severe CD",
    ),
  },
  {
    criterion:
      "Trial and inadequate response or intolerance to two preferred agents",
    kind: "required",
    citation: cite(
      ANTHEM_DOC,
      "p.3",
      "Individual has had a trial and inadequate response or intolerance to TWO preferred agents. (A trial of multiple products with the same active ingredient counts as a trial of ONE preferred agent)",
    ),
  },
  {
    criterion:
      "Documentation of why the individual cannot use all preferred agents",
    kind: "required",
    citation: cite(
      ANTHEM_DOC,
      "p.3",
      "Information is provided for why the individual is unable to use ALL preferred agents (or all remaining preferred agents if individual has tried any preferred agent)",
    ),
  },
  {
    criterion:
      "Not requested in combination with a prohibited immunomodulator",
    kind: "required",
    citation: cite(
      ANTHEM_DOC,
      "p.3",
      "In combination with oral or topical JAK inhibitors, apremilast, ozanimod, etrasimod, deucravacitinib, or any of the following biologic immunomodulators: Other TNF antagonists, IL-23 inhibitors, IL-17 inhibitors",
    ),
  },
  {
    criterion: "Reviewed under the medical benefit plan",
    kind: "supporting",
    citation: cite(
      ANTHEM_DOC,
      "p.2",
      "When a drug is being reviewed for coverage under a member's medical benefit plan or is otherwise subject to clinical review (including prior authorization), the following criteria will be used",
    ),
  },
  {
    criterion: "Individual is maintained on a stable dose",
    kind: "supporting",
    citation: cite(
      ANTHEM_DOC,
      "p.3",
      "Individual has been receiving and is maintained on a stable dose of Stelara/ Imuldosa/Otulfi/Pyzchiva/Selarsdi/ Starjemza/ Steqeyma/Wezlana/Yesintek",
    ),
  },
  {
    criterion:
      "Confirmation of clinically significant improvement or stabilization",
    kind: "supporting",
    citation: cite(
      ANTHEM_DOC,
      "p.3",
      "There is confirmation of clinically significant improvement or stabilization in clinical signs and symptoms of the disease",
    ),
  },
  {
    criterion:
      "Diagnosis falls under the commercial step therapy pathway",
    kind: "supporting",
    citation: cite(
      ANTHEM_DOC,
      "p.3",
      "Commercial requests for diagnosis of Crohn's Disease or Ulcerative Colitis: Requests for a non-preferred ustekinumab agent may be approved when the following criteria are met",
    ),
  },
];

function build(slots: Eight<Slot>, fills: Eight<Fill>): Criterion[] {
  return slots.map((slot, i) => ({
    criterion: slot.criterion,
    kind: slot.kind,
    status: fills[i].status,
    evidence: fills[i].evidence,
    citation: slot.citation,
  }));
}

/** The band rule, run over the mock criteria so fixtures cannot contradict it. */
function grade(criteria: Criterion[]): { score: number; band: Band } {
  const required = criteria.filter((c) => c.kind === "required");
  const supporting = criteria.filter((c) => c.kind === "supporting");
  const pool = (list: Criterion[], points: number) =>
    list.length === 0
      ? 0
      : (list.filter((c) => c.status === "MET").length * points) / list.length;
  const score = Math.round(pool(required, 80) + pool(supporting, 20));
  const anyContradicted = required.some((c) => c.status === "NOT_MET");
  const allMet = required.every((c) => c.status === "MET");
  // A record where nothing required could be determined either way is
  // undocumented, not contradicted, so the score floor does not apply. Closing
  // it on points alone would be an adverse determination with no human in it,
  // which is the thing the Submit gate exists to prevent.
  const nothingDetermined = !required.some(
    (c) => c.status === "MET" || c.status === "NOT_MET",
  );
  const band: Band =
    anyContradicted
      ? "NOT_QUALIFIED"
      : nothingDetermined
        ? "NEEDS_REVIEW"
        : score < 60
          ? "NOT_QUALIFIED"
          : score >= 85 && allMet
            ? "QUALIFIES"
            : "NEEDS_REVIEW";
  return { score, band };
}

function result(criteria: Criterion[], summary: string): QualifyResult {
  return { ...grade(criteria), criteria, summary };
}

// ---------------------------------------------------------------------------
// Infusion centers and appointment slots
// ---------------------------------------------------------------------------

const CENTERS: Center[] = [
  {
    name: "Hudson Infusion Center",
    address: "1400 Hudson Street, New York, NY 10014",
    distance_mi: 2.4,
    offers_drug: true,
  },
  {
    name: "Gramercy Day Hospital",
    address: "1900 East 20th Street, New York, NY 10003",
    distance_mi: 3.1,
    offers_drug: false,
  },
  {
    name: "Riverside Infusion Suite",
    address: "1750 Riverside Drive, New York, NY 10024",
    distance_mi: 4.6,
    offers_drug: true,
  },
  {
    name: "Brooklyn Heights Infusion",
    address: "1620 Montague Street, Brooklyn, NY 11201",
    distance_mi: 6.8,
    offers_drug: true,
  },
];

const CENTER_PHONE = "(555) 014-2200";

/** A slot `days` out at 14:30 UTC, so the booked card always reads forward. */
function slot(days: number): string {
  const d = new Date(START + days * 86_400_000);
  d.setUTCHours(14, 30, 0, 0);
  return d.toISOString();
}

function appointmentAt(center: Center, starts_at: string, booked_at: string): Appointment {
  return {
    center_name: center.name,
    address: center.address,
    phone: CENTER_PHONE,
    starts_at,
    booked_at,
  };
}

function callSummary(center: string): CallInfo {
  return {
    call_id: "call-demo-" + center.toLowerCase().replace(/[^a-z]+/g, "-"),
    status: "completed",
    outcome: "completed",
    summary: {
      criteria_summary: [
        "Infusion chair confirmed for the requested week.",
        "Clinic stocks ustekinumab for intravenous induction.",
        "Referral and authorization number required at check in.",
      ],
      unresolved_questions: [
        "Confirm whether a pre-infusion lab draw happens on site.",
      ],
      public_sources: [
        { label: "Scheduling desk, confirmed on the call", url: null },
      ],
    },
  };
}

// ---------------------------------------------------------------------------
// Event histories
// ---------------------------------------------------------------------------

function shift(ts: string, minutes: number): string {
  return new Date(Date.parse(ts) + minutes * 60_000).toISOString();
}

function searchEvents(ts: string, chosen: Center, starts_at: string): Event[] {
  return [
    {
      ts,
      kind: "center_search",
      message: "Searched infusion centers offering ustekinumab.",
      payload: { centers: CENTERS, chosen: chosen.name },
    },
    {
      ts: shift(ts, 1),
      kind: "slot_identified",
      message: "Open slot found at " + chosen.name + ".",
      payload: { center_name: chosen.name, starts_at },
    },
  ];
}

/* The same seven beats mockSchedule plays live, so a seeded booked patient and
 * one that books during the demo have the same timeline, not just the same
 * card. Any beat added to one has to be added to the other. */
function callEvents(ts: string, chosen: Center): Event[] {
  return [
    { ts, kind: "call_created", message: "Call queued to " + chosen.name + "." },
    {
      ts: shift(ts, 1),
      kind: "call_dialing",
      message: "Dialing " + chosen.name + " at " + CENTER_PHONE + ".",
    },
    {
      ts: shift(ts, 2),
      kind: "call_screening",
      message: "Reached the clinic line. Waiting for scheduling.",
    },
    {
      ts: shift(ts, 3),
      kind: "call_active",
      message: "Agent in conversation with the scheduling desk.",
    },
    {
      ts: shift(ts, 4),
      kind: "call_summarizing",
      message: "Confirming the slot and closing the call.",
    },
    {
      ts: shift(ts, 5),
      kind: "call_completed",
      message: "Call completed. Slot confirmed.",
    },
  ];
}

type History = {
  created: string;
  decided: string | null;
  booked: string | null;
  payer_name: string;
  outcome: Decision["outcome"] | null;
  band: Band;
  chosen?: Center;
  starts_at?: string;
};

/** A plausible timeline for a seeded row, so completed cards open onto the same
 *  tracker a live patient does. */
function history(h: History): Event[] {
  const events: Event[] = [
    { ts: h.created, kind: "stage_change", message: "Case opened at intake." },
    {
      ts: shift(h.created, 2),
      kind: "stage_change",
      message: "Matched to the " + h.payer_name + " commercial policy.",
    },
  ];
  // A patient still at intake has not run qualification, so the history stops.
  if (!h.decided) return events;

  events.push({
    ts: shift(h.created, 3),
    kind: "qualify_started",
    message: "Qualification run against the payer policy.",
  });
  events.push({
    ts: h.decided,
    kind: "qualify_done",
    message: "Qualification complete. Band " + h.band + ".",
  });

  if (h.outcome === "NOT_SUBMITTED") {
    events.push({
      ts: shift(h.decided, 1),
      kind: "stage_change",
      message: "Not submitted. A required criterion was contradicted by the record.",
    });
    return events;
  }

  // Space the payer beats across the gap that actually elapsed, so no seeded
  // event ever lands after the booking or in the future.
  const decidedAt = h.decided;
  const end = Date.parse(h.booked ?? now());
  const span = Math.max(end - Date.parse(decidedAt), 6 * 60_000);
  const at = (fraction: number) => shift(decidedAt, (span * fraction) / 60_000);

  // Every stage change carries a stage_change event, matching the backend and
  // the live path below, so a seeded timeline and a timeline that landed during
  // the demo read the same.
  events.push({
    ts: at(0.08),
    kind: "submitted",
    message: "Prior authorization request submitted to " + h.payer_name + ".",
  });
  events.push({
    ts: at(0.08),
    kind: "stage_change",
    message: "Submitted to " + h.payer_name + ".",
  });

  if (h.outcome === "PAYER_DENIED") {
    events.push({
      ts: at(0.6),
      kind: "payer_decision",
      message: h.payer_name + " denied the request.",
    });
    events.push({
      ts: at(0.6),
      kind: "stage_change",
      message: "Denied by payer.",
    });
    return events;
  }

  events.push({
    ts: at(0.45),
    kind: "payer_decision",
    message: h.payer_name + " approved the request.",
  });
  events.push({
    ts: at(0.45),
    kind: "stage_change",
    message: "Ready to schedule.",
  });

  if (!h.booked || !h.chosen || !h.starts_at) return events;
  const dialAt = shift(h.booked, -6);
  events.push(
    ...searchEvents(shift(dialAt, -2), h.chosen, h.starts_at),
    { ts: dialAt, kind: "stage_change", message: "Placing the call." },
    ...callEvents(dialAt, h.chosen),
    { ts: h.booked, kind: "stage_change", message: "Appointment booked." },
    {
      ts: h.booked,
      kind: "booked",
      message: "Appointment booked at " + h.chosen.name + ".",
    },
  );
  return events;
}

// ---------------------------------------------------------------------------
// The eleven patients
// ---------------------------------------------------------------------------

const USTEKINUMAB = { drug: "ustekinumab", brand_name: "Stelara" };

const met = (evidence: string): Fill => ({ status: "MET", evidence });
const notMet = (evidence: string): Fill => ({ status: "NOT_MET", evidence });
const unknown = (evidence: string): Fill => ({ status: "UNKNOWN", evidence });

const HUDSON = CENTERS[0];
const RIVERSIDE = CENTERS[2];
const BROOKLYN = CENTERS[3];

function seed(): PatientDetail[] {
  const elenaCriteria = build(UHC_SLOTS, [
    met("Colonoscopy 2025-11-04 reports moderately to severely active ileocolonic disease."),
    met("Prednisone 40 mg tapered over 10 weeks with recurrence at taper."),
    met("Steqeyma from 2025-12-02 to 2026-03-24, 16 weeks, minimal response documented."),
    met("Ordered by Dr. Alice Nunez, gastroenterology."),
    met("Order is a single intravenous induction dose."),
    met("Weight based induction dose of 390 mg for 62 kg."),
    met("No concurrent targeted immunomodulator on the medication list."),
    met("Authorization requested for one induction dose."),
  ]);
  const tomasCriteria = build(UHC_SLOTS, [
    met("Diagnosis of moderately active Crohn's disease with perianal involvement."),
    met("Azathioprine 150 mg daily for 6 months without remission."),
    met("Wezlana from 2025-10-14 to 2026-02-10, 17 weeks, residual disease activity."),
    met("Ordered by Dr. Peter Vance, gastroenterology."),
    met("Order is a single intravenous induction dose."),
    unknown("Induction weight not recorded on the order."),
    met("No concurrent targeted immunomodulator on the medication list."),
    met("Authorization requested for one induction dose."),
  ]);
  const juneCriteria = build(ANTHEM_SLOTS, [
    met("Patient is 44 years old with moderate to severe Crohn's disease."),
    met("Trial of Wezlana and Steqeyma, both with inadequate response."),
    met("Prescriber letter explains intolerance to the remaining preferred agents."),
    met("No JAK inhibitor or biologic immunomodulator on the medication list."),
    met("Request submitted under the medical benefit."),
    met("Stable dose maintained since 2026-01-08."),
    unknown("No follow up note attached for the improvement statement."),
    met("Commercial plan, Crohn's disease diagnosis."),
  ]);
  const victorCriteria = build(UHC_SLOTS, [
    met("Diagnosis of severely active Crohn's disease confirmed 2025-09-18."),
    met("Methotrexate 25 mg weekly for 4 months, no remission."),
    unknown("No biosimilar trial found in the record."),
    met("Ordered by Dr. Alice Nunez, gastroenterology."),
    met("Order is a single intravenous induction dose."),
    met("Weight based induction dose of 390 mg for 71 kg."),
    met("No concurrent targeted immunomodulator on the medication list."),
    met("Authorization requested for one induction dose."),
  ]);
  const priyaCriteria = build(ANTHEM_SLOTS, [
    met("Patient is 38 years old with moderate Crohn's disease."),
    unknown("Only one preferred agent trial is documented."),
    met("Prescriber letter attached."),
    met("No prohibited combination on the medication list."),
    met("Request submitted under the medical benefit."),
    unknown("Dosing history not attached."),
    met("Improvement noted at the 2026-02-11 visit."),
    met("Commercial plan, Crohn's disease diagnosis."),
  ]);
  const samCriteria = build(UHC_SLOTS, [
    met("Diagnosis of moderately active Crohn's disease."),
    unknown("Conventional therapy history not attached to the request."),
    met("Yesintek from 2025-08-05 to 2025-12-01, 17 weeks, minimal response."),
    met("Ordered by Dr. Peter Vance, gastroenterology."),
    met("Order is a single intravenous induction dose."),
    unknown("Induction weight not recorded on the order."),
    met("No concurrent targeted immunomodulator on the medication list."),
    unknown("Duration of the authorization request not stated."),
  ]);
  const anaCriteria = build(UHC_SLOTS, [
    met("Diagnosis of moderately active Crohn's disease."),
    met("Budesonide 9 mg daily for 12 weeks with recurrence."),
    notMet("Steqeyma course ran 6 weeks, below the 14 week floor in the policy."),
    met("Ordered by Dr. Alice Nunez, gastroenterology."),
    met("Order is a single intravenous induction dose."),
    unknown("Induction weight not recorded on the order."),
    unknown("Medication list not attached."),
    unknown("Duration of the authorization request not stated."),
  ]);
  const owenCriteria = build(UHC_SLOTS, [
    met("Diagnosis of moderately active Crohn's disease."),
    notMet("No conventional therapy trial in the record. First line request."),
    notMet("Wezlana course ran 4 weeks, below the 14 week floor in the policy."),
    met("Ordered by Dr. Peter Vance, gastroenterology."),
    met("Order is a single intravenous induction dose."),
    met("Weight based induction dose of 520 mg for 88 kg."),
    met("No concurrent targeted immunomodulator on the medication list."),
    unknown("Duration of the authorization request not stated."),
  ]);
  const raviCriteria = build(UHC_SLOTS, [
    met("Diagnosis of moderately to severely active Crohn's disease, 2025-10-02."),
    met("Prednisone 40 mg tapered over 8 weeks with recurrence at taper."),
    met("Wezlana from 2025-11-10 to 2026-03-16, 18 weeks, minimal clinical response."),
    met("Ordered by Dr. Alice Nunez, gastroenterology."),
    met("Order is a single intravenous induction dose."),
    met("Weight based induction dose of 390 mg for 68 kg."),
    met("No concurrent targeted immunomodulator on the medication list."),
    unknown("Duration of the authorization request not stated."),
  ]);

  const patients: PatientDetail[] = [
    {
      id: "pt-dana",
      name: "Dana Ortiz",
      ...payerOf(UHC_DOC),
      ...USTEKINUMAB,
      stage: "intake",
      score: null,
      band: null,
      decision: null,
      appointment: null,
      evidence_id: null,
      created_at: ago(42),
      decided_at: null,
      booked_at: null,
      updated_at: ago(42),
      record: {
        age: 34,
        weight_kg: 61,
        height_cm: 165,
        diagnosis: "Crohn's disease, moderately to severely active, ileocolonic",
        treatment_history: [
          "Prednisone 40 mg daily, tapered over 10 weeks, recurrence at taper",
          "Wezlana (ustekinumab-auub) 2026-04-06 to 2026-06-08, discontinued at 9 weeks",
        ],
        screening: {
          tuberculosis: "Negative QuantiFERON, 2026-03-28",
          hepatitis_b: "Non-reactive, 2026-03-28",
          pregnancy: "Not applicable",
        },
        provider: {
          name: "Dr. Alice Nunez",
          npi: "1234567890",
          specialty: "Gastroenterology",
        },
      },
      result: null,
      call: null,
      events: history({
        created: ago(42),
        decided: null,
        booked: null,
        payer_name: UHC_DOC.payer_name,
        outcome: null,
        band: "NEEDS_REVIEW",
      }),
    },
    {
      id: "pt-marcus",
      name: "Marcus Webb",
      ...payerOf(UHC_DOC),
      ...USTEKINUMAB,
      stage: "intake",
      score: null,
      band: null,
      decision: null,
      appointment: null,
      evidence_id: null,
      created_at: ago(27),
      decided_at: null,
      booked_at: null,
      updated_at: ago(27),
      record: {
        age: 47,
        weight_kg: 74,
        height_cm: 178,
        diagnosis: "Crohn's disease, moderately to severely active, ileal",
        treatment_history: [
          "Azathioprine 150 mg daily for 7 months, no remission",
          "Wezlana (ustekinumab-auub) 2026-01-19 to 2026-05-11, 16 weeks, minimal response",
        ],
        screening: {
          tuberculosis: "Negative QuantiFERON, 2026-01-05",
          hepatitis_b: "Non-reactive, 2026-01-05",
          pregnancy: "Not applicable",
        },
        provider: {
          name: "Dr. Peter Vance",
          npi: "1111111111",
          specialty: "Gastroenterology",
        },
      },
      result: null,
      call: null,
      events: history({
        created: ago(27),
        decided: null,
        booked: null,
        payer_name: UHC_DOC.payer_name,
        outcome: null,
        band: "NEEDS_REVIEW",
      }),
    },
    {
      id: "pt-ravi",
      name: "Ravi Patel",
      ...payerOf(UHC_DOC),
      ...USTEKINUMAB,
      stage: "scheduling",
      ...grade(raviCriteria),
      decision: {
        outcome: "APPROVED",
        source: "payer",
        reasoning:
          "UnitedHealthcare approved the induction dose under the commercial medical benefit drug policy.",
        decided_at: ago(18),
      },
      appointment: null,
      evidence_id: "ev-pt-ravi",
      created_at: ago(258),
      decided_at: ago(18),
      booked_at: null,
      updated_at: ago(18),
      record: {
        age: 52,
        weight_kg: 68,
        height_cm: 172,
        diagnosis: "Crohn's disease, moderately to severely active, colonic",
        treatment_history: [
          "Prednisone 40 mg daily, tapered over 8 weeks, recurrence at taper",
          "Wezlana (ustekinumab-auub) 2025-11-10 to 2026-03-16, 18 weeks, minimal response",
        ],
        screening: {
          tuberculosis: "Negative QuantiFERON, 2025-10-20",
          hepatitis_b: "Non-reactive, 2025-10-20",
          pregnancy: "Not applicable",
        },
        provider: {
          name: "Dr. Alice Nunez",
          npi: "1234567890",
          specialty: "Gastroenterology",
        },
      },
      result: result(
        raviCriteria,
        "All four required criteria are met. The Wezlana course ran 18 weeks with minimal clinical response, which clears the 14 week floor.",
      ),
      call: null,
      events: history({
        created: ago(258),
        decided: ago(18),
        booked: null,
        payer_name: UHC_DOC.payer_name,
        outcome: "APPROVED",
        band: grade(raviCriteria).band,
      }),
    },
    completed({
      id: "pt-elena",
      name: "Elena Park",
      doc: UHC_DOC,
      criteria: elenaCriteria,
      summary:
        "All four required criteria are met. The Steqeyma course ran 16 weeks with minimal response.",
      outcome: "APPROVED",
      created: 4300,
      decided: 4054,
      booked: 2490,
      chosen: HUDSON,
      starts_at: slot(-1),
      record: {
        age: 39,
        weight_kg: 62,
        height_cm: 168,
        diagnosis: "Crohn's disease, moderately to severely active, ileocolonic",
        treatment_history: [
          "Prednisone 40 mg daily, tapered over 10 weeks",
          "Steqeyma (ustekinumab-stba) 2025-12-02 to 2026-03-24, 16 weeks",
        ],
        provider: {
          name: "Dr. Alice Nunez",
          npi: "1234567890",
          specialty: "Gastroenterology",
        },
      },
    }),
    completed({
      id: "pt-tomas",
      name: "Tomas Rivera",
      doc: UHC_DOC,
      criteria: tomasCriteria,
      summary:
        "All four required criteria are met. Induction weight is undocumented, which costs a supporting point only.",
      outcome: "APPROVED",
      created: 3680,
      decided: 3446,
      booked: 1581,
      chosen: RIVERSIDE,
      starts_at: slot(1),
      record: {
        age: 45,
        weight_kg: 80,
        height_cm: 180,
        diagnosis: "Crohn's disease, moderately active, with perianal involvement",
        treatment_history: [
          "Azathioprine 150 mg daily for 6 months",
          "Wezlana (ustekinumab-auub) 2025-10-14 to 2026-02-10, 17 weeks",
        ],
        provider: {
          name: "Dr. Peter Vance",
          npi: "1111111111",
          specialty: "Gastroenterology",
        },
      },
    }),
    completed({
      id: "pt-june",
      name: "June Okafor",
      doc: ANTHEM_DOC,
      criteria: juneCriteria,
      summary:
        "Both preferred agent trials are documented and the prescriber letter covers the remaining agents.",
      outcome: "APPROVED",
      created: 2900,
      decided: 2594,
      booked: 120,
      chosen: BROOKLYN,
      starts_at: slot(3),
      record: {
        age: 44,
        weight_kg: 66,
        height_cm: 170,
        diagnosis: "Crohn's disease, moderate to severe",
        treatment_history: [
          "Wezlana (ustekinumab-auub) 2025-06-02 to 2025-10-13",
          "Steqeyma (ustekinumab-stba) 2025-10-20 to 2026-01-05",
        ],
        provider: {
          name: "Dr. Maya Iyer",
          npi: "1234567891",
          specialty: "Gastroenterology",
        },
      },
    }),
    completed({
      id: "pt-victor",
      name: "Victor Hale",
      doc: UHC_DOC,
      criteria: victorCriteria,
      summary:
        "The biosimilar trial requirement is undocumented. Conduit routed this case to specialist review before submission.",
      outcome: "PAYER_DENIED",
      created: 4250,
      decided: 3806,
      reasoning:
        "No documented 14-week trial of a preferred biosimilar (Wezlana, Steqeyma, Yesintek); commercial policy p.2",
      record: {
        age: 58,
        weight_kg: 71,
        height_cm: 175,
        diagnosis: "Crohn's disease, severely active, ileal",
        treatment_history: ["Methotrexate 25 mg weekly for 4 months"],
        provider: {
          name: "Dr. Alice Nunez",
          npi: "1234567890",
          specialty: "Gastroenterology",
        },
      },
    }),
    completed({
      id: "pt-priya",
      name: "Priya Raman",
      doc: ANTHEM_DOC,
      criteria: priyaCriteria,
      summary:
        "Only one preferred agent trial is documented. Conduit routed this case to specialist review before submission.",
      outcome: "PAYER_DENIED",
      created: 2810,
      decided: 2618,
      reasoning:
        "Trial of two preferred ustekinumab agents not evidenced; clinical criteria CC-0063 p.3",
      record: {
        age: 38,
        weight_kg: 59,
        height_cm: 162,
        diagnosis: "Crohn's disease, moderate",
        treatment_history: ["Wezlana (ustekinumab-auub) 2025-11-03 to 2026-02-09"],
        provider: {
          name: "Dr. Maya Iyer",
          npi: "1234567891",
          specialty: "Gastroenterology",
        },
      },
    }),
    completed({
      id: "pt-sam",
      name: "Sam Whitfield",
      doc: UHC_DOC,
      criteria: samCriteria,
      summary:
        "Conventional therapy history is undocumented. Conduit routed this case to specialist review before submission.",
      outcome: "PAYER_DENIED",
      created: 1700,
      decided: 1334,
      reasoning:
        "Conventional therapy failure not evidenced; commercial policy p.2",
      record: {
        age: 61,
        weight_kg: 84,
        height_cm: 181,
        diagnosis: "Crohn's disease, moderately active",
        treatment_history: ["Yesintek (ustekinumab-kfce) 2025-08-05 to 2025-12-01"],
        provider: {
          name: "Dr. Peter Vance",
          npi: "1111111111",
          specialty: "Gastroenterology",
        },
      },
    }),
    completed({
      id: "pt-ana",
      name: "Ana Beltran",
      doc: UHC_DOC,
      criteria: anaCriteria,
      summary:
        "The Steqeyma course ran 6 weeks against a 14 week floor. The record contradicts the requirement.",
      outcome: "NOT_SUBMITTED",
      created: 2510,
      decided: 2384,
      reasoning:
        "Biosimilar trial ran 6 weeks against the 14 week floor in the commercial policy p.2",
      record: {
        age: 29,
        weight_kg: 55,
        height_cm: 160,
        diagnosis: "Crohn's disease, moderately active",
        treatment_history: [
          "Budesonide 9 mg daily for 12 weeks",
          "Steqeyma (ustekinumab-stba) 2026-03-02 to 2026-04-13, 6 weeks",
        ],
        provider: {
          name: "Dr. Alice Nunez",
          npi: "1234567890",
          specialty: "Gastroenterology",
        },
      },
    }),
    completed({
      id: "pt-owen",
      name: "Owen Marsh",
      doc: UHC_DOC,
      criteria: owenCriteria,
      summary:
        "No conventional therapy trial and a 4 week biosimilar course. Two required criteria are contradicted by the record.",
      outcome: "NOT_SUBMITTED",
      created: 1490,
      decided: 1262,
      reasoning:
        "No conventional therapy trial, and the biosimilar course ran 4 weeks against the 14 week floor; commercial policy p.2",
      record: {
        age: 33,
        weight_kg: 88,
        height_cm: 184,
        diagnosis: "Crohn's disease, moderately active",
        treatment_history: ["Wezlana (ustekinumab-auub) 2026-05-04 to 2026-06-01, 4 weeks"],
        provider: {
          name: "Dr. Peter Vance",
          npi: "1111111111",
          specialty: "Gastroenterology",
        },
      },
    }),
  ];
  return patients;
}

type CompletedSpec = {
  id: string;
  name: string;
  doc: typeof UHC_DOC;
  criteria: Criterion[];
  summary: string;
  outcome: Decision["outcome"];
  created: number;
  decided: number;
  booked?: number;
  chosen?: Center;
  starts_at?: string;
  reasoning?: string;
  record: Omit<PatientDetail["record"], "screening"> & {
    screening?: Record<string, string>;
  };
};

const STAGE_FOR: Record<Decision["outcome"], Stage> = {
  APPROVED: "booked",
  PAYER_DENIED: "payer_denied",
  NOT_SUBMITTED: "not_qualified",
};

function completed(spec: CompletedSpec): PatientDetail {
  const created_at = ago(spec.created);
  const decided_at = ago(spec.decided);
  const booked_at = spec.booked === undefined ? null : ago(spec.booked);
  const graded = grade(spec.criteria);
  const decision: Decision = {
    outcome: spec.outcome,
    source: spec.outcome === "NOT_SUBMITTED" ? "conduit" : "payer",
    reasoning:
      spec.reasoning ??
      spec.doc.payer_name +
        " approved the induction dose under the commercial medical benefit drug policy.",
    decided_at,
  };
  return {
    id: spec.id,
    name: spec.name,
    payer: spec.doc.payer,
    payer_name: spec.doc.payer_name,
    plan_type: spec.doc.plan_type,
    ...USTEKINUMAB,
    stage: STAGE_FOR[spec.outcome],
    score: graded.score,
    band: graded.band,
    decision,
    appointment:
      booked_at && spec.chosen && spec.starts_at
        ? appointmentAt(spec.chosen, spec.starts_at, booked_at)
        : null,
    evidence_id: "ev-" + spec.id,
    created_at,
    decided_at,
    booked_at,
    updated_at: booked_at ?? decided_at,
    record: {
      ...spec.record,
      screening: spec.record.screening ?? {
        tuberculosis: "Negative QuantiFERON, on file",
        hepatitis_b: "Non-reactive, on file",
      },
    },
    result: result(spec.criteria, spec.summary),
    call: spec.chosen ? callSummary(spec.chosen.name) : null,
    events: history({
      created: created_at,
      decided: decided_at,
      booked: booked_at,
      payer_name: spec.doc.payer_name,
      outcome: spec.outcome,
      band: graded.band,
      chosen: spec.chosen,
      starts_at: spec.starts_at,
    }),
  };
}

// ---------------------------------------------------------------------------
// Live qualification scripts (the two stories that stream)
// ---------------------------------------------------------------------------

type Script = { text: string; criteria: Criterion[]; summary: string };

function scripts(): Record<string, Script> {
  return {
    "pt-dana": {
      text: [
        "Reading UnitedHealthcare commercial medical benefit drug policy 2026D0045AB, ustekinumab.",
        "Criterion 1, diagnosis. The record states moderately to severely active ileocolonic Crohn's disease, confirmed by colonoscopy. MET.",
        "Criterion 2, conventional therapy. Prednisone 40 mg was tapered over 10 weeks with recurrence at taper. MET.",
        "Criterion 3, preferred biosimilar trial. The policy requires documentation of a trial of at least 14 weeks.",
        "The record shows Wezlana from 2026-04-06 to 2026-06-08. That is 9 weeks.",
        "The record contradicts the requirement rather than leaving it undocumented. NOT_MET.",
        "Criterion 4, prescriber. Ordered by Dr. Alice Nunez, gastroenterology. MET.",
        "Supporting criteria: single intravenous induction dose, FDA labeled dosing, no combination immunomodulator, one induction dose. All MET.",
        "A required criterion is contradicted, so the request stops before submission.",
      ].join(" "),
      criteria: build(UHC_SLOTS, [
        met("Colonoscopy 2026-03-21 reports moderately to severely active ileocolonic disease."),
        met("Prednisone 40 mg tapered over 10 weeks with recurrence at taper."),
        notMet(
          "Wezlana course ran 2026-04-06 to 2026-06-08, 9 weeks against the 14 week floor in the policy.",
        ),
        met("Ordered by Dr. Alice Nunez, gastroenterology."),
        met("Order is a single intravenous induction dose."),
        met("Weight based induction dose of 390 mg for 61 kg."),
        met("No concurrent targeted immunomodulator on the medication list."),
        met("Authorization requested for one induction dose."),
      ]),
      summary:
        "The preferred biosimilar trial ran 9 weeks against the 14 week floor the policy sets. The record contradicts the requirement, so the request is not submitted.",
    },
    "pt-marcus": {
      text: [
        "Reading UnitedHealthcare commercial medical benefit drug policy 2026D0045AB, ustekinumab.",
        "Criterion 1, diagnosis. Moderately to severely active ileal Crohn's disease is documented. MET.",
        "Criterion 2, conventional therapy. Azathioprine 150 mg daily for 7 months without remission. MET.",
        "Criterion 3, preferred biosimilar trial. Wezlana from 2026-01-19 to 2026-05-11 is 16 weeks with minimal clinical response and residual disease activity.",
        "That clears the 14 week floor. MET.",
        "Criterion 4, prescriber. Ordered by Dr. Peter Vance, gastroenterology. MET.",
        "Supporting criteria: single intravenous induction dose, weight based dosing at 74 kg, no combination immunomodulator, one induction dose. All MET.",
        "Every required criterion is met. The case is ready for a specialist to submit.",
      ].join(" "),
      criteria: build(UHC_SLOTS, [
        met("Diagnosis of moderately to severely active ileal Crohn's disease."),
        met("Azathioprine 150 mg daily for 7 months without remission."),
        met(
          "Wezlana course ran 2026-01-19 to 2026-05-11, 16 weeks with minimal clinical response.",
        ),
        met("Ordered by Dr. Peter Vance, gastroenterology."),
        met("Order is a single intravenous induction dose."),
        met("Weight based induction dose of 520 mg for 74 kg."),
        met("No concurrent targeted immunomodulator on the medication list."),
        met("Authorization requested for one induction dose."),
      ]),
      summary:
        "All four required criteria are met and every supporting criterion is documented. The 16 week Wezlana course clears the policy's 14 week floor.",
    },
  };
}

// ---------------------------------------------------------------------------
// Module state and the mock endpoints
// ---------------------------------------------------------------------------

let PATIENTS = seed();
const SCRIPTS = scripts();
const LIVE_RUN = new Set<string>();
const SCHEDULING = new Set<string>();
const FAIL_NEXT_CALL = new Set<string>();
const TIMERS: ReturnType<typeof setTimeout>[] = [];

/** Arms the failed-call beat for one run so the retry path is rehearsable. */
export function mockFailNextCall(id: string) {
  FAIL_NEXT_CALL.add(id);
}

export type Conflict = { ok: false; message: string };

const TERMINAL = new Set<Stage>(TERMINAL_STAGES);

function find(id: string): PatientDetail | undefined {
  return PATIENTS.find((p) => p.id === id);
}

function touch(p: PatientDetail, kind: string, message: string, payload?: Event["payload"]) {
  p.updated_at = now();
  p.events = [...p.events, { ts: p.updated_at, kind, message, payload }];
}

/** Cards carry no record, result, call, or events: the list endpoint on the
 *  real API does not send them, so the mock must not either. */
function toCard(p: PatientDetail): Card {
  return {
    id: p.id,
    name: p.name,
    payer: p.payer,
    payer_name: p.payer_name,
    drug: p.drug,
    brand_name: p.brand_name,
    plan_type: p.plan_type,
    stage: p.stage,
    score: p.score,
    band: p.band,
    decision: p.decision,
    appointment: p.appointment,
    evidence_id: p.evidence_id,
    created_at: p.created_at,
    decided_at: p.decided_at,
    booked_at: p.booked_at,
    updated_at: p.updated_at,
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function later(ms: number, fn: () => void) {
  TIMERS.push(setTimeout(fn, ms));
}

export async function mockPatients(): Promise<{ active: Card[]; completed: Card[] }> {
  const cards = PATIENTS.map(toCard);
  const at = (value: string | null) => (value ? Date.parse(value) : 0);
  return {
    active: cards
      .filter((c) => !TERMINAL.has(c.stage))
      .sort((a, b) => at(a.created_at) - at(b.created_at)),
    // Newest determination first, matching the backend, so a card that finishes
    // on stage arrives at the top rather than wherever the seed order put it.
    completed: cards
      .filter((c) => TERMINAL.has(c.stage))
      .sort((a, b) => at(b.decided_at) - at(a.decided_at)),
  };
}

export async function mockEvent(
  id: string,
  kind: string,
  message: string,
  payload?: Record<string, unknown>,
): Promise<{ ok: true } | Conflict> {
  const p = find(id);
  if (!p) return { ok: false, message: "Unknown patient." };
  touch(p, kind, message, payload);
  return { ok: true };
}

export async function mockPatient(id: string): Promise<PatientDetail | null> {
  const p = find(id);
  return p ? structuredClone(p) : null;
}

export async function mockQualify(
  id: string,
  onToken: (text: string) => void,
  signal?: AbortSignal,
): Promise<{ ok: true } | Conflict> {
  const p = find(id);
  if (!p) return { ok: false, message: "Unknown patient." };
  if (p.stage !== "intake" && p.stage !== "policy_matched") {
    return { ok: false, message: "Qualification has already run for this patient." };
  }
  const script = SCRIPTS[id];
  if (!script) {
    return { ok: false, message: "No policy match for this patient yet." };
  }

  // Rewound if the run is abandoned. Without this an abort leaves the patient at
  // `qualifying`, where no button renders and the poll never stops, and the only
  // way out is a reload that reseeds every other patient too.
  const entryStage = p.stage;
  const entryEvents = p.events.length;
  p.stage = "qualifying";
  touch(p, "stage_change", "Qualification running against the payer policy.");
  touch(p, "qualify_started", "Qualification running against the payer policy.");

  // One word at a time at roughly the cadence of local Gemma decoding.
  for (const token of script.text.split(/(?<=\s)/)) {
    if (signal?.aborted) {
      p.stage = entryStage;
      p.events = p.events.slice(0, entryEvents);
      return { ok: false, message: "Qualification was cancelled." };
    }
    onToken(token);
    await sleep(40);
  }

  const qualified = result(script.criteria, script.summary);
  const decidedAt = now();
  p.result = qualified;
  p.score = qualified.score;
  p.band = qualified.band;
  p.evidence_id = "ev-" + p.id;
  LIVE_RUN.add(p.id);
  touch(p, "qualify_done", "Qualification complete. Band " + qualified.band + ".");

  if (qualified.band === "NOT_QUALIFIED") {
    p.stage = "not_qualified";
    p.decided_at = decidedAt;
    p.decision = {
      outcome: "NOT_SUBMITTED",
      source: "conduit",
      reasoning: qualified.summary,
      decided_at: decidedAt,
    };
    touch(
      p,
      "stage_change",
      "Not submitted. A required criterion is contradicted by the record.",
    );
  } else {
    p.stage = "review";
    touch(p, "stage_change", "Ready for specialist review.");
  }
  return { ok: true };
}

export async function mockSubmit(
  id: string,
  action: "submit" | "do_not_submit",
): Promise<{ ok: true; stage: Stage } | Conflict> {
  const p = find(id);
  if (!p) return { ok: false, message: "Unknown patient." };
  if (p.stage !== "review") {
    return { ok: false, message: "This case is not waiting for a submission decision." };
  }
  if (action === "submit") {
    p.stage = "submitted";
    touch(
      p,
      "submitted",
      "Clinical criteria passed. Prior authorization request submitted to " +
        p.payer_name +
        ".",
    );
    touch(p, "stage_change", "Submitted to " + p.payer_name + ".");
  } else {
    const decidedAt = now();
    p.stage = "not_qualified";
    p.decided_at = decidedAt;
    p.decision = {
      outcome: "NOT_SUBMITTED",
      source: "conduit",
      reasoning: "Specialist elected not to submit this request.",
      decided_at: decidedAt,
    };
    touch(p, "stage_change", "Not submitted. Held by the specialist.");
  }
  return { ok: true, stage: p.stage };
}

export async function mockSchedule(id: string): Promise<{ ok: true } | Conflict> {
  const p = find(id);
  if (!p) return { ok: false, message: "Unknown patient." };
  // The active-call gate is checked first. Once the dial moves the stage off
  // `scheduling` the stage test below would answer with the approval message,
  // which is false: the payer did approve, a call is simply already running.
  if (SCHEDULING.has(id) || p.stage === "calling") {
    return { ok: false, message: "A call is already active for this patient." };
  }
  if (p.stage !== "scheduling" || p.decision?.outcome !== "APPROVED") {
    return {
      ok: false,
      message: "Scheduling opens once the payer has approved the request.",
    };
  }
  SCHEDULING.add(id);

  const chosen = HUDSON;
  const starts_at = slot(2);
  // PRD 14 lists the failed-call retry as a rehearsed mitigation, so the beat
  // has to be reachable without a backend. Set CONDUIT_MOCK_CALL_FAILS to walk
  // it; the Schedule button re-arms from the call_failed event.
  const failThisRun = FAIL_NEXT_CALL.delete(id);

  touch(p, "center_search", "Searched infusion centers offering ustekinumab.", {
    centers: CENTERS,
    chosen: chosen.name,
  });
  later(1500, () =>
    touch(p, "slot_identified", "Open slot found at " + chosen.name + ".", {
      center_name: chosen.name,
      starts_at,
    }),
  );
  later(3000, () => touch(p, "call_created", "Call queued to " + chosen.name + "."));
  later(4500, () => {
    p.stage = "calling";
    touch(p, "stage_change", "Placing the call.");
    touch(p, "call_dialing", "Dialing " + chosen.name + " at " + CENTER_PHONE + ".");
  });
  later(7000, () =>
    touch(p, "call_screening", "Reached the clinic line. Waiting for scheduling."),
  );

  if (failThisRun) {
    later(9000, () => {
      // Back to the resting stage so the human can retry, which is what the
      // backend does on a failed outcome.
      p.stage = "scheduling";
      touch(p, "stage_change", "Call did not complete. Ready to try again.");
      touch(p, "call_failed", "Call ended without reaching scheduling: no-answer.");
      SCHEDULING.delete(id);
    });
    return { ok: true };
  }

  later(9000, () =>
    touch(p, "call_active", "Agent in conversation with the scheduling desk."),
  );
  later(12000, () =>
    touch(p, "call_summarizing", "Confirming the slot and closing the call."),
  );
  later(14500, () => {
    const bookedAt = now();
    p.call = callSummary(chosen.name);
    p.appointment = appointmentAt(chosen, starts_at, bookedAt);
    p.booked_at = bookedAt;
    p.stage = "booked";
    touch(p, "call_completed", "Call completed. Slot confirmed.");
    touch(p, "stage_change", "Appointment booked.");
    touch(p, "booked", "Appointment booked at " + chosen.name + ".");
    SCHEDULING.delete(id);
  });
  return { ok: true };
}

export async function mockEvidence(evidenceId: string): Promise<Evidence | null> {
  const p = PATIENTS.find((x) => x.evidence_id === evidenceId);
  if (!p || !p.result) return null;
  const doc = p.payer === "anthem" ? DOCS.anthem : DOCS.uhc;
  return {
    patient_id: p.id,
    patient_name: p.name,
    determination: DETERMINATION_TEXT[p.result.band],
    score: p.result.score,
    band: p.result.band,
    score_rule: SCORE_RULE,
    criteria: p.result.criteria,
    doc_sha256: doc.sha256,
    doc_url: doc.doc_url,
    // Nothing inferred in mock mode, so the receipt must not claim Gemma ran.
    model: LIVE_RUN.has(p.id) ? "mock" : "seeded",
    created_at: p.decided_at ?? p.updated_at,
    disclaimer: DISCLAIMER,
  };
}

/* Band-derived, matching the backend, so a receipt reads the same in rehearsal
 * as it does live. The receipt certifies what Conduit found against the policy,
 * which is the band; the payer's later answer is on the determination block.
 * The backend writes NOT_QUALIFIED with a comma, which PRD 6.2 spells with a
 * spaced hyphen. Hyphen is correct and the backend has to follow. */
const DETERMINATION_TEXT: Record<Band, string> = {
  QUALIFIES: "Meets the payer's published criteria for submission",
  NEEDS_REVIEW: "Unresolved criteria, routed to specialist review",
  NOT_QUALIFIED: "Not submitted - criteria not met",
};

export async function mockHealth(): Promise<Health> {
  // Mock mode has no backend to be healthy. Reporting "ok" here would make the
  // one honest diagnostic on the about page incapable of ever showing trouble.
  return { gemma: "unreachable", search: "unavailable" };
}

export async function mockReset(): Promise<{ ok: true }> {
  while (TIMERS.length > 0) clearTimeout(TIMERS.pop());
  SCHEDULING.clear();
  FAIL_NEXT_CALL.clear();
  LIVE_RUN.clear();
  PATIENTS = seed();
  return { ok: true };
}
