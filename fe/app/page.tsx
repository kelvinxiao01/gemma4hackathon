"use client";

import { useEffect, useState } from "react";
import { health, type Health } from "@/lib/api";

// The shell the copilot UI mounts into. It is not a marketing page: the health
// row is the real thing, and an unreachable dot at 15:29 is worth more than any
// other diagnostic on this screen.

const BANDS = [
  { name: "HIGH", cls: "bg-band-high text-background", note: "answered from policy, cited" },
  { name: "MEDIUM", cls: "bg-band-medium text-background", note: "partial or ambiguous match" },
  { name: "NEEDS_REVIEW", cls: "bg-band-review text-background", note: "a person decides this one" },
  { name: "REJECTED_INPUT", cls: "bg-band-rejected text-background", note: "not a policy question" },
];

function Mark({ size = 40 }: { size?: number }) {
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

export default function Home() {
  // Doubles as the Gemma warm-up. A browser reports the identical generic
  // "Failed to fetch" for a dead server and for a missing CORS header, so name
  // the state rather than surfacing the exception.
  const [h, setH] = useState<Health | "pending" | "unreachable">("pending");
  useEffect(() => {
    health().then(setH, () => setH("unreachable"));
  }, []);

  const up = typeof h === "object" && h.gemma === "ok" && h.search === "ok";
  const dot = h === "pending" ? "bg-muted" : up ? "bg-band-high" : "bg-band-medium";
  const label =
    h === "pending"
      ? "checking backend"
      : h === "unreachable"
        ? "backend unreachable at 127.0.0.1:8000"
        : `gemma ${h.gemma} / search ${h.search}`;

  return (
    <div className="flex flex-1 bg-background text-foreground">
      {/* The copper rule is the conduit run: one unbroken line down the page. */}
      <div className="w-[3px] shrink-0 bg-accent" />

      <main className="flex flex-1 flex-col justify-center gap-16 px-8 py-24 sm:px-16 lg:px-24">
        <header className="rise flex items-center gap-4">
          <Mark />
          <span className="wordmark text-2xl">CONDUIT</span>
        </header>

        <div className="rise max-w-3xl" style={{ animationDelay: "90ms" }}>
          <h1 className="text-5xl font-medium leading-[1.05] tracking-tight sm:text-7xl">
            Speed that
            <br />
            holds up.
          </h1>
          <p className="mt-8 max-w-xl text-lg leading-relaxed text-muted">
            Prior authorization runs slowly because every step has to be
            defensible. Conduit clears the run by making the proof automatic. The
            answer arrives with its payer policy citation already attached, and
            the patient context never leaves this machine.
          </p>
        </div>

        <div className="rise flex flex-wrap gap-x-10 gap-y-4" style={{ animationDelay: "180ms" }}>
          {BANDS.map((b) => (
            <div key={b.name} className="flex items-center gap-3">
              <span className={`verbatim px-2 py-1 text-[11px] leading-none ${b.cls}`}>
                {b.name}
              </span>
              <span className="text-sm text-muted">{b.note}</span>
            </div>
          ))}
        </div>

        <footer
          className="rise verbatim flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted"
          style={{ animationDelay: "270ms" }}
        >
          <span className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${dot}`} aria-hidden />
            {label}
          </span>
          <span className="text-accent-text">gemma4:e4b, on this device</span>
        </footer>
      </main>
    </div>
  );
}
