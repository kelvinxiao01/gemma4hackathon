"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";
import {
  messageOf,
  patient as fetchPatient,
  qualify,
  schedule,
  submit,
} from "@/lib/client";
import type { Center, Event, PatientDetail, Stage } from "@/lib/types";
import { TERMINAL_STAGES } from "@/lib/types";
import {
  BandChip,
  Disclaimer,
  Masthead,
  Notice,
  OutcomeBadge,
  Page,
  STAGE_LABEL,
  StatusChip,
  when,
} from "@/app/ui";

// The stepper axis never changes shape. The location search and the phone call
// are event sequences inside `scheduling` and `calling`, not extra stages.
const AXIS: Stage[] = [
  "intake",
  "policy_matched",
  "qualifying",
  "review",
  "submitted",
  "payer_decision",
  "scheduling",
  "calling",
  "booked",
];

// Where each terminal branch leaves the happy path.
const BRANCH_FROM: Record<"not_qualified" | "payer_denied", Stage> = {
  not_qualified: "qualifying",
  payer_denied: "payer_decision",
};

const TERMINAL = new Set<Stage>(TERMINAL_STAGES);

const CALL_LABEL: Record<string, string> = {
  call_created: "Call queued",
  call_dialing: "Dialing the clinic",
  call_screening: "Reached the clinic line",
  call_active: "Agent in conversation",
  call_summarizing: "Summarizing the call",
  call_completed: "Call completed",
  call_failed: "Call did not complete",
};

function Check() {
  return (
    <svg viewBox="0 0 12 12" width={10} height={10} aria-hidden>
      <path
        d="M2 6.3 L4.6 9 L10 3"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
      />
    </svg>
  );
}

function isBranch(stage: Stage): stage is "not_qualified" | "payer_denied" {
  return stage === "not_qualified" || stage === "payer_denied";
}

function Stepper({ stage }: { stage: Stage }) {
  const branch = isBranch(stage) ? stage : null;
  const forkAt = branch ? AXIS.indexOf(BRANCH_FROM[branch]) : -1;
  const current = branch ? forkAt : AXIS.indexOf(stage);

  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-3">
      {AXIS.map((s, i) => {
        const done = i < current || (branch !== null && i <= forkAt);
        const here = !branch && i === current;
        return (
          <li
            key={s}
            className="flex items-center gap-2"
            aria-current={here ? "step" : undefined}
          >
            <span
              className={
                "flex h-5 w-5 items-center justify-center text-[11px] leading-none " +
                (done
                  ? "bg-band-high text-background"
                  : here
                    ? "bg-accent-fill text-background"
                    : "bg-surface text-muted")
              }
              aria-hidden
            >
              {done ? <Check /> : i + 1}
            </span>
            {/* The markers carry the state visually and are aria-hidden, so
                without this a screen reader gets nine identical stage names. */}
            <span className="sr-only">
              {done ? "completed" : here ? "current step" : "not started"}
            </span>
            <span
              className={
                "text-xs " + (here ? "font-medium" : done ? "" : "text-muted")
              }
            >
              {STAGE_LABEL[s]}
            </span>
            {i === forkAt ? (
              <span className="verbatim ml-1 bg-band-medium px-2 py-1 text-[11px] leading-none text-background">
                {STAGE_LABEL[stage]}
              </span>
            ) : i < AXIS.length - 1 ? (
              <span className="h-px w-5 bg-[var(--bone-dim)]" aria-hidden />
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

function lastOf(events: Event[], kinds: (kind: string) => boolean): Event | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (kinds(events[i].kind)) return events[i];
  }
  return null;
}

/** center_search payload, validated field by field so a malformed event renders
 *  nothing instead of crashing the tracker. One unusable center drops that row
 *  and not the panel: four good centers beat a blank where the story goes. */
function readCenters(event: Event | null): { centers: Center[]; chosen: string } | null {
  const payload = event?.payload;
  if (!payload) return null;
  const { centers, chosen } = payload;
  if (!Array.isArray(centers) || typeof chosen !== "string") return null;
  const parsed: Center[] = [];
  for (const raw of centers) {
    const c: unknown = raw;
    if (typeof c !== "object" || c === null) continue;
    if (!("name" in c && "address" in c && "distance_mi" in c && "offers_drug" in c)) {
      continue;
    }
    const { name, address, distance_mi, offers_drug } = c;
    if (
      typeof name !== "string" ||
      typeof address !== "string" ||
      // typeof alone is not enough: NaN is a number, and it would scramble the
      // distance sort and render "NaN mi".
      typeof distance_mi !== "number" ||
      !Number.isFinite(distance_mi) ||
      typeof offers_drug !== "boolean"
    ) {
      continue;
    }
    parsed.push({ name, address, distance_mi, offers_drug });
  }
  return { centers: parsed, chosen };
}

function readSlot(event: Event | null): { center_name: string; starts_at: string } | null {
  const payload = event?.payload;
  if (!payload) return null;
  const { center_name, starts_at } = payload;
  if (typeof center_name !== "string" || typeof starts_at !== "string") return null;
  return { center_name, starts_at };
}

function CenterSearch({ events }: { events: Event[] }) {
  const search = readCenters(lastOf(events, (k) => k === "center_search"));
  if (!search) return null;
  const slot = readSlot(lastOf(events, (k) => k === "slot_identified"));
  const offering = search.centers.filter((c) => c.offers_drug);
  const skipped = search.centers.length - offering.length;

  return (
    <section className="mt-6 bg-surface p-5">
      <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
        Location search
      </h2>
      {offering.length === 0 ? (
        // The search ran and produced nothing renderable. Saying so beats an
        // empty panel, which is indistinguishable from a hung page.
        <p className="mt-4 text-sm">
          No infusion center in the response offers this drug.
        </p>
      ) : null}
      <ul className="mt-4 space-y-2">
        {[...offering]
          .sort((a, b) => a.distance_mi - b.distance_mi)
          .map((c) => {
            const chosen = c.name === search.chosen;
            return (
              <li
                key={c.name}
                className={
                  "flex flex-wrap items-baseline justify-between gap-2 px-3 py-2 " +
                  (chosen ? "bg-background border-l-2 border-accent" : "")
                }
              >
                <span className={chosen ? "font-medium" : ""}>{c.name}</span>
                <span className="text-sm text-muted">{c.address}</span>
                <span className="verbatim text-sm">{c.distance_mi} mi</span>
              </li>
            );
          })}
      </ul>
      {skipped > 0 ? (
        <p className="mt-3 text-xs text-muted">
          {skipped} nearer center filtered out: does not offer this drug.
        </p>
      ) : null}
      {slot ? (
        <p className="mt-4 text-sm">
          Slot identified at {slot.center_name}, {when(slot.starts_at)}.
        </p>
      ) : null}
    </section>
  );
}

function CallChip({ events }: { events: Event[] }) {
  const event = lastOf(events, (k) => k.startsWith("call_"));
  if (!event) return null;
  const failed = event.kind === "call_failed";
  const done = event.kind === "call_completed";
  return (
    <div className="mt-4 flex flex-wrap items-center gap-3">
      <span
        className={
          "verbatim px-2 py-1 text-[11px] leading-none " +
          (failed
            ? "bg-band-medium text-background"
            : done
              ? "bg-band-high text-background"
              : "bg-band-review text-background")
        }
      >
        {CALL_LABEL[event.kind] ?? event.kind}
      </span>
      <span className="text-sm">{event.message}</span>
    </div>
  );
}

export default function Tracker({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [p, setP] = useState<PatientDetail | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [tokens, setTokens] = useState("");
  const [busy, setBusy] = useState(false);
  const streamRef = useRef<HTMLPreElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Guards every write, because refresh is also called from the three action
  // handlers, whose requests can outlive the page.
  const aliveRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchPatient(id);
      if (!aliveRef.current) return null;
      setP(next);
      setError(null);
      return next;
    } catch (err) {
      if (aliveRef.current) setError(messageOf(err));
      return null;
    } finally {
      if (aliveRef.current) setLoaded(true);
    }
  }, [id]);

  // Self-scheduling rather than setInterval: an interval does not wait for the
  // previous request, so a slow response landing after a fast later one would
  // walk the stepper backward. Re-arms only while the case can still move.
  useEffect(() => {
    aliveRef.current = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const next = await refresh();
      if (!aliveRef.current) return;
      if (next && TERMINAL.has(next.stage)) return;
      timer = setTimeout(tick, 2000);
    };
    void tick();
    return () => {
      aliveRef.current = false;
      clearTimeout(timer);
    };
  }, [refresh]);

  // Leaving the page cancels the stream, which is what stops the backend from
  // generating into a reader nobody is holding.
  useEffect(() => {
    const controller = new AbortController();
    abortRef.current = controller;
    return () => controller.abort();
  }, [id]);

  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [tokens]);

  const runQualify = async () => {
    setBusy(true);
    setNotice(null);
    setTokens("");
    try {
      const outcome = await qualify(
        id,
        (text) => setTokens((prior) => prior + text),
        abortRef.current?.signal,
      );
      if (!outcome.ok && aliveRef.current) setNotice(outcome.message);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (aliveRef.current) setNotice(messageOf(err));
    } finally {
      if (aliveRef.current) {
        setBusy(false);
        await refresh();
      }
    }
  };

  const runSubmit = async (action: "submit" | "do_not_submit") => {
    setBusy(true);
    setNotice(null);
    try {
      const outcome = await submit(id, action);
      if (!outcome.ok) setNotice(outcome.message);
    } catch (err) {
      setNotice(messageOf(err));
    } finally {
      setBusy(false);
      await refresh();
    }
  };

  const runSchedule = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const outcome = await schedule(id);
      if (!outcome.ok) setNotice(outcome.message);
    } catch (err) {
      setNotice(messageOf(err));
    } finally {
      setBusy(false);
      await refresh();
    }
  };

  if (!loaded) {
    return (
      <Page>
        <Masthead />
        <p className="mt-10 text-sm text-muted">Loading patient.</p>
      </Page>
    );
  }

  if (!p) {
    return (
      <Page>
        <Masthead />
        <h1 className="mt-10 text-2xl font-medium">Patient not found</h1>
        <p className="mt-3 text-sm text-muted">
          No case with id <span className="verbatim">{id}</span>.
        </p>
        {error ? <div className="mt-4"><Notice text={error} /></div> : null}
        <Link href="/" className="mt-6 inline-block text-sm text-accent-text hover:underline">
          Back to the caseload
        </Link>
      </Page>
    );
  }

  const canQualify = p.stage === "intake" || p.stage === "policy_matched";
  const canDecide = p.stage === "review";
  // A failed call has to re-arm Schedule even if the backend leaves the stage
  // where the dial put it, because the retry is a rehearsed demo beat.
  const lastCall = lastOf(p.events, (k) => k.startsWith("call_"));
  const callFailed = lastCall?.kind === "call_failed";
  const canSchedule =
    p.decision?.outcome === "APPROVED" &&
    (p.stage === "scheduling" || callFailed);
  const button =
    "px-4 py-2 text-sm transition-opacity disabled:opacity-50 disabled:cursor-not-allowed";

  return (
    <Page>
      <Masthead />

      <div className="mt-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-medium tracking-tight">{p.name}</h1>
          <p className="verbatim mt-2 text-xs text-muted">
            {p.payer_name} / {p.brand_name} ({p.drug}) / {p.plan_type}
          </p>
        </div>
        <div className="flex items-center gap-4">
          {p.band ? <BandChip band={p.band} score={p.score} /> : null}
          {p.evidence_id ? (
            <Link
              href={"/evidence/" + p.evidence_id}
              className="text-sm text-accent-text hover:underline"
            >
              evidence receipt
            </Link>
          ) : null}
        </div>
      </div>

      <div className="mt-6 bg-surface p-5">
        <Stepper stage={p.stage} />
      </div>

      {error ? <div className="mt-4"><Notice text={error} /></div> : null}
      {notice ? <div className="mt-4"><Notice text={notice} /></div> : null}

      {canQualify || canDecide || canSchedule ? (
        <div className="mt-6 flex flex-wrap items-center gap-3">
          {canQualify ? (
            <button
              type="button"
              onClick={runQualify}
              disabled={busy}
              className={button + " bg-accent-fill text-background"}
            >
              {busy ? "Running qualification" : "Run qualification"}
            </button>
          ) : null}
          {canDecide ? (
            <>
              <button
                type="button"
                onClick={() => runSubmit("submit")}
                disabled={busy}
                className={button + " bg-accent-fill text-background"}
              >
                Submit to {p.payer_name}
              </button>
              <button
                type="button"
                onClick={() => runSubmit("do_not_submit")}
                disabled={busy}
                className={button + " bg-surface"}
              >
                Do not submit
              </button>
            </>
          ) : null}
          {canSchedule ? (
            <>
              <button
                type="button"
                onClick={runSchedule}
                disabled={busy}
                className={button + " bg-accent-fill text-background"}
              >
                {callFailed ? "Try the call again" : "Schedule appointment"}
              </button>
              {callFailed && lastCall ? (
                <span className="text-sm text-muted">{lastCall.message}</span>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}

      {tokens || busy ? (
        <section className="mt-6">
          <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
            Gemma reasoning
          </h2>
          <pre
            ref={streamRef}
            className="verbatim mt-3 max-h-64 overflow-auto whitespace-pre-wrap bg-[var(--graphite)] p-4 text-sm leading-relaxed text-[var(--bone)]"
          >
            {tokens}
            {busy ? <span className="text-[var(--copper-lift)]">|</span> : null}
          </pre>
          <Disclaimer className="mt-2" />
        </section>
      ) : null}

      {p.result ? (
        <section className="mt-8">
          <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
            Criteria
          </h2>
          <p className="mt-3 max-w-3xl">{p.result.summary}</p>
          <div className="mt-4 space-y-3">
            {p.result.criteria.map((c) => (
              <article key={c.criterion} className="bg-surface p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <span className="font-medium">{c.criterion}</span>
                  <span className="flex items-center gap-2">
                    <span className="verbatim text-[11px] text-muted">
                      {c.kind}
                    </span>
                    <StatusChip status={c.status} />
                  </span>
                </div>
                <p className="mt-2 text-sm">{c.evidence}</p>
                <figure className="mt-3 border-l-2 border-accent pl-3">
                  <blockquote className="text-sm text-muted">
                    {c.citation.quote}
                  </blockquote>
                  <figcaption className="verbatim mt-2 text-xs">
                    {c.citation.payer_name} {c.citation.plan_type},{" "}
                    {/* A document with no pagination sends its url as the ref,
                        which overruns this caption without break-all. */}
                    <span className="break-all">{c.citation.ref}</span>{" "}
                    <a
                      href={c.citation.doc_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-accent-text hover:underline"
                    >
                      source
                    </a>
                  </figcaption>
                </figure>
              </article>
            ))}
          </div>
          <Disclaimer className="mt-3" />
        </section>
      ) : null}

      <CenterSearch events={p.events} />
      <CallChip events={p.events} />

      {p.decision ? (
        <section className="mt-8 bg-surface p-5">
          <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
            Determination
          </h2>
          <div className="mt-3">
            <OutcomeBadge decision={p.decision} />
          </div>
          <p className="mt-3 max-w-3xl">{p.decision.reasoning}</p>
          <p className="verbatim mt-2 text-xs text-muted">
            {when(p.decision.decided_at)}, source {p.decision.source}
          </p>
          <Disclaimer className="mt-3" />
        </section>
      ) : null}

      {p.appointment ? (
        <section className="mt-6 bg-surface p-5">
          <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
            Appointment
          </h2>
          <p className="mt-3 text-lg">{p.appointment.center_name}</p>
          <p className="text-sm">{p.appointment.address}</p>
          <p className="verbatim mt-2 text-sm">
            {when(p.appointment.starts_at)} / {p.appointment.phone}
          </p>
          <p className="verbatim mt-2 text-xs text-muted">
            booked {when(p.appointment.booked_at)}
          </p>
        </section>
      ) : null}

      {p.call?.summary ? (
        <section className="mt-6">
          <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
            Call notes
          </h2>
          <ul className="mt-3 space-y-1 text-sm">
            {p.call.summary.criteria_summary.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {p.call.summary.unresolved_questions.length > 0 ? (
            <>
              <p className="mt-3 text-xs uppercase tracking-wide text-muted">
                Left open
              </p>
              <ul className="mt-1 space-y-1 text-sm">
                {p.call.summary.unresolved_questions.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </>
          ) : null}
          {p.call.summary.public_sources.length > 0 ? (
            <>
              <p className="mt-3 text-xs uppercase tracking-wide text-muted">
                Sources
              </p>
              <ul className="mt-1 space-y-1 text-sm">
                {p.call.summary.public_sources.map((s, i) => (
                  <li key={(s.url ?? s.label ?? "") + i}>
                    {s.url ? (
                      <a
                        href={s.url}
                        target="_blank"
                        rel="noreferrer"
                        className="break-all text-accent-text hover:underline"
                      >
                        {s.label ?? s.url}
                      </a>
                    ) : (
                      (s.label ?? "unattributed")
                    )}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          <Disclaimer className="mt-3" />
        </section>
      ) : null}

      <section className="mt-8 grid gap-6 lg:grid-cols-2">
        <div>
          <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
            Patient record
          </h2>
          <dl className="mt-3 space-y-1 text-sm">
            <Row label="Diagnosis" value={p.record.diagnosis} />
            <Row
              label="Demographics"
              value={
                p.record.age +
                " years, " +
                p.record.weight_kg +
                " kg, " +
                p.record.height_cm +
                " cm"
              }
            />
            <Row
              label="Provider"
              value={
                p.record.provider.name +
                ", " +
                p.record.provider.specialty +
                ", NPI " +
                p.record.provider.npi
              }
            />
          </dl>
          <p className="mt-3 text-xs uppercase tracking-wide text-muted">
            Treatment history
          </p>
          <ul className="mt-1 space-y-1 text-sm">
            {p.record.treatment_history.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {Object.keys(p.record.screening).length > 0 ? (
            <>
              <p className="mt-3 text-xs uppercase tracking-wide text-muted">
                Screening
              </p>
              <dl className="mt-1 space-y-1 text-sm">
                {Object.entries(p.record.screening).map(([k, v]) => (
                  <Row key={k} label={k.replace(/_/g, " ")} value={v} />
                ))}
              </dl>
            </>
          ) : null}
        </div>

        <div>
          <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
            Timeline
          </h2>
          <ol className="mt-3 space-y-2">
            {p.events.map((e, i) => (
              <li key={e.ts + e.kind + i} className="flex gap-3 text-sm">
                <span className="verbatim shrink-0 text-xs text-muted">
                  {when(e.ts)}
                </span>
                <span>{e.message}</span>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <Link
        href="/"
        className="mt-12 inline-block text-sm text-accent-text hover:underline"
      >
        Back to the caseload
      </Link>
      <Disclaimer className="mt-6" />
    </Page>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-3">
      <dt className="w-32 shrink-0 text-xs uppercase tracking-wide text-muted">
        {label}
      </dt>
      <dd>{value}</dd>
    </div>
  );
}
