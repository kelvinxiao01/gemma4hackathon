// Shared rendering. The label rules here are legally load-bearing: a
// conduit-source outcome never carries the word denied, and every surface that
// shows generated content carries the disclaimer.

import Link from "next/link";
import type { Band, Criterion, Decision, Stage } from "@/lib/types";
import { DISCLAIMER } from "@/lib/types";

export function Mark({ size = 40 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} role="img" aria-label="Conduit">
      {/* Wall, then conductor. The conductor stops short of the mouth. */}
      <path
        d="M19 4.5 H9 A2 2 0 0 0 7 6.5 V17.5 A2 2 0 0 0 9 19.5 H19"
        fill="none"
        stroke="currentColor"
        strokeWidth={3.5}
      />
      <path
        d="M17 9.25 H13 A1 1 0 0 0 12 10.25 V13.75 A1 1 0 0 0 13 14.75 H17"
        fill="none"
        stroke="var(--accent)"
        strokeWidth={2.5}
      />
    </svg>
  );
}

export function Disclaimer({ className = "" }: { className?: string }) {
  return <p className={"text-xs text-muted " + className}>{DISCLAIMER}</p>;
}

/** The copper run down the left edge, shared by every page. */
export function Page({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1 bg-background text-foreground">
      <div className="w-[3px] shrink-0 bg-accent" />
      {/* min-w-0 so an unbreakable token (a long policy url) cannot push the
          page body into horizontal scroll on a projector. */}
      <main className="min-w-0 flex-1 px-6 py-8 sm:px-10 lg:px-14">{children}</main>
    </div>
  );
}

export function Masthead({ facts }: { facts?: string }) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-4">
      <Link href="/" className="flex items-center gap-3">
        <Mark size={32} />
        <span className="wordmark text-xl">CONDUIT</span>
      </Link>
      <div className="verbatim flex items-center gap-5 text-xs text-muted">
        {facts ? <span>{facts}</span> : null}
        <Link href="/about" className="text-accent-text hover:underline">
          about
        </Link>
      </div>
    </header>
  );
}

export const STAGE_LABEL: Record<Stage, string> = {
  intake: "Intake",
  policy_matched: "Policy matched",
  qualifying: "Qualifying",
  review: "Specialist review",
  submitted: "Submitted",
  payer_decision: "Payer decision",
  scheduling: "Scheduling",
  calling: "Calling",
  booked: "Booked",
  not_qualified: "Not submitted",
  payer_denied: "Denied by payer",
};

/**
 * The outcome label law. NOT_SUBMITTED is a Conduit pre-submission screen out
 * and says so with a spaced hyphen; the word denied belongs to the payer alone.
 */
export function outcomeLabel(decision: Decision): string {
  if (decision.outcome === "APPROVED") return "Approved";
  if (decision.outcome === "PAYER_DENIED") return "Denied by payer";
  return "Not submitted - criteria not met";
}

function outcomeClass(decision: Decision): string {
  if (decision.outcome === "APPROVED") return "bg-band-high text-background";
  if (decision.outcome === "PAYER_DENIED") return "bg-band-review text-background";
  return "bg-band-medium text-background";
}

export function OutcomeBadge({ decision }: { decision: Decision }) {
  return (
    <span
      className={
        "verbatim px-2 py-1 text-[11px] leading-none " + outcomeClass(decision)
      }
    >
      {outcomeLabel(decision)}
    </span>
  );
}

const BAND_CLASS: Record<Band, string> = {
  QUALIFIES: "bg-band-high text-background",
  NEEDS_REVIEW: "bg-band-review text-background",
  NOT_QUALIFIED: "bg-band-medium text-background",
};

export function BandChip({ band, score }: { band: Band; score: number | null }) {
  return (
    <span className="flex items-center gap-2">
      <span
        className={"verbatim px-2 py-1 text-[11px] leading-none " + BAND_CLASS[band]}
      >
        {band}
      </span>
      {score === null ? null : (
        <span className="verbatim text-sm">{score} / 100</span>
      )}
    </span>
  );
}

const STATUS_CLASS: Record<Criterion["status"], string> = {
  MET: "bg-band-high text-background",
  NOT_MET: "bg-band-medium text-background",
  UNKNOWN: "bg-band-rejected text-background",
};

export function StatusChip({ status }: { status: Criterion["status"] }) {
  return (
    <span
      className={
        "verbatim inline-block px-2 py-1 text-[11px] leading-none " +
        STATUS_CLASS[status]
      }
    >
      {status}
    </span>
  );
}

/** Every error and every 409 reaches the user through this, so it announces. */
export function Notice({ text }: { text: string }) {
  return (
    <p
      role="status"
      aria-live="polite"
      className="border-l-2 border-accent bg-surface px-3 py-2 text-sm"
    >
      {text}
    </p>
  );
}

// --- formatting -----------------------------------------------------------

export function when(iso: string | null): string {
  if (!iso) return "-";
  const at = new Date(iso);
  // An unparseable timestamp is an absent input, not the words "Invalid Date"
  // on a projector.
  if (Number.isNaN(at.getTime())) return "-";
  return at.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function duration(ms: number): string {
  const minutes = Math.round(ms / 60_000);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h >= 24) {
    const days = Math.floor(h / 24);
    return days + "d " + (h % 24) + "h";
  }
  return h + "h " + String(m).padStart(2, "0") + "m";
}
