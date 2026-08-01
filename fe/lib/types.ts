// The copilot contract, shared with the backend. These shapes are frozen: a
// field renamed here is a field the API will never send.

export type Stage =
  | "intake"
  | "policy_matched"
  | "qualifying"
  | "review"
  | "submitted"
  | "payer_decision"
  | "scheduling"
  | "calling"
  | "booked"
  | "not_qualified"
  | "payer_denied";

export type Band = "QUALIFIES" | "NEEDS_REVIEW" | "NOT_QUALIFIED";

export type Outcome = "APPROVED" | "PAYER_DENIED" | "NOT_SUBMITTED";

// `ref` is printed exactly as it arrives. Never derive a page number from
// another field: the citation is the product.
export type Citation = {
  payer_name: string;
  plan_type: string;
  ref: string;
  doc_url: string;
  quote: string;
};

// NOT_MET means the record contradicts the requirement and stops the case.
// UNKNOWN means the requirement is undocumented and routes to a human.
export type Criterion = {
  criterion: string;
  kind: "required" | "supporting";
  status: "MET" | "NOT_MET" | "UNKNOWN";
  evidence: string;
  citation: Citation;
};

export type Decision = {
  outcome: Outcome;
  source: "conduit" | "payer";
  reasoning: string;
  decided_at: string;
};

export type Appointment = {
  center_name: string;
  address: string;
  phone: string;
  starts_at: string;
  booked_at: string;
};

// kinds: stage_change, qualify_started, qualify_done, submitted, payer_decision,
//        center_search, slot_identified, call_created, call_dialing,
//        call_screening, call_active, call_summarizing, call_completed,
//        call_failed, booked, note
export type Event = {
  ts: string;
  kind: string;
  message: string;
  payload?: Record<string, unknown>;
};

export type CallInfo = {
  call_id: string;
  status: string;
  outcome: string | null;
  summary: {
    criteria_summary: string[];
    unresolved_questions: string[];
    public_sources: { label: string | null; url: string | null }[];
  } | null;
};

export type Card = {
  id: string;
  name: string;
  payer: string;
  payer_name: string;
  drug: string;
  brand_name: string;
  plan_type: string;
  stage: Stage;
  score: number | null;
  band: Band | null;
  decision: Decision | null;
  appointment: Appointment | null;
  evidence_id: string | null;
  created_at: string;
  decided_at: string | null;
  booked_at: string | null;
  updated_at: string;
};

export type PatientRecord = {
  age: number;
  weight_kg: number;
  height_cm: number;
  diagnosis: string;
  treatment_history: string[];
  screening: Record<string, string>;
  provider: { name: string; npi: string; specialty: string };
};

export type QualifyResult = {
  score: number;
  band: Band;
  criteria: Criterion[];
  summary: string;
};

export type PatientDetail = Card & {
  record: PatientRecord;
  result: QualifyResult | null;
  call: CallInfo | null;
  events: Event[];
};

export type Evidence = {
  patient_id: string;
  patient_name: string;
  determination: string;
  score: number;
  band: Band;
  score_rule: string;
  criteria: Criterion[];
  doc_sha256: string;
  doc_url: string;
  model: string;
  created_at: string;
  disclaimer: string;
};

export type Health = {
  gemma: "ok" | "unreachable";
  search: "ok" | "unavailable";
};

// The center_search event payload, read off Event.payload when kind matches.
export type Center = {
  name: string;
  address: string;
  distance_mi: number;
  offers_drug: boolean;
};

export const TERMINAL_STAGES: Stage[] = [
  "booked",
  "not_qualified",
  "payer_denied",
];

export const DISCLAIMER =
  "Decision support for human review. Not a coverage determination.";

export const SCORE_RULE =
  "Twenty points per required criterion met and five per supporting criterion, " +
  "to one hundred. Any required criterion the record contradicts stops the " +
  "request before submission. Any required criterion left undocumented routes " +
  "the case to specialist review.";
