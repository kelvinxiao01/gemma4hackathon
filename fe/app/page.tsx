"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { messageOf, patients } from "@/lib/client";
import type { Card } from "@/lib/types";
import {
  BandChip,
  Disclaimer,
  Masthead,
  Notice,
  OutcomeBadge,
  Page,
  STAGE_LABEL,
  duration,
  when,
} from "@/app/ui";

const CORPUS_FACTS = "15 policy documents, 4 payers, 528 indexed chunks";

/** Median of a list that may be empty. Tiles show a hyphen rather than a zero
 *  when there is nothing to measure yet. */
function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1
    ? sorted[mid]
    : (sorted[mid - 1] + sorted[mid]) / 2;
}

function spanMs(from: string | null, to: string | null): number | null {
  if (!from || !to) return null;
  const ms = Date.parse(to) - Date.parse(from);
  return Number.isFinite(ms) && ms >= 0 ? ms : null;
}

function isNumber(value: number | null): value is number {
  return value !== null;
}

type Tile = { label: string; value: string; note: string };

/** Every tile is derived from the rows on screen, so they tick as cards land. */
function tiles(rows: Card[]): Tile[] {
  const toDetermination = rows
    .map((r) => spanMs(r.created_at, r.decided_at))
    .filter(isNumber);
  const toBooked = rows
    .map((r) => spanMs(r.decided_at, r.booked_at))
    .filter(isNumber);
  const gaps = rows.filter(
    (r) => r.decision?.outcome === "NOT_SUBMITTED",
  ).length;
  const waiting = rows.filter((r) => r.stage === "review").length;

  const determination = median(toDetermination);
  const booked = median(toBooked);
  // No rows means no inputs, which is a hyphen rather than a confident zero.
  const count = (n: number) => (rows.length === 0 ? "-" : String(n));

  return [
    {
      label: "Median time to determination",
      value: determination === null ? "-" : duration(determination),
      note: "CMS-0057-F allows 7 days",
    },
    {
      label: "Gaps caught before submission",
      value: count(gaps),
      // Names the payer, because the counterfactual is the payer's action. The
      // determination itself is Conduit's, and Conduit never denies anything.
      note: "would likely have been denied by the payer",
    },
    {
      label: "Awaiting specialist review",
      value: count(waiting),
      note: "a human decides these",
    },
    {
      label: "Approved to booked",
      value: booked === null ? "-" : duration(booked),
      note: "payer approval to an infusion chair",
    },
  ];
}

function PatientCard({ card, delay }: { card: Card; delay: number }) {
  return (
    <Link
      href={"/patients/" + card.id}
      className="rise block bg-surface p-4 transition-transform hover:-translate-y-[2px]"
      style={{ animationDelay: delay + "ms" }}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <span className="text-lg font-medium">{card.name}</span>
        {card.decision ? (
          <OutcomeBadge decision={card.decision} />
        ) : (
          <span className="verbatim bg-background px-2 py-1 text-[11px] leading-none">
            {STAGE_LABEL[card.stage]}
          </span>
        )}
      </div>

      <p className="verbatim mt-2 text-xs text-muted">
        {card.payer_name} / {card.brand_name} ({card.drug}) / {card.plan_type}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        {card.band ? <BandChip band={card.band} score={card.score} /> : null}
        {card.evidence_id ? (
          <span className="text-xs text-accent-text">evidence receipt</span>
        ) : null}
      </div>

      {card.appointment ? (
        <p className="mt-3 text-sm">
          {card.appointment.center_name}, {when(card.appointment.starts_at)}
        </p>
      ) : null}
    </Link>
  );
}

function Section({ title, cards }: { title: string; cards: Card[] }) {
  return (
    <section className="mt-10">
      <h2 className="text-sm uppercase tracking-[0.18em] text-muted">
        {title} ({cards.length})
      </h2>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {cards.map((c, i) => (
          <PatientCard key={c.id} card={c} delay={i * 40} />
        ))}
      </div>
    </section>
  );
}

export default function Caseload() {
  const [active, setActive] = useState<Card[]>([]);
  const [completed, setCompleted] = useState<Card[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  // Self-scheduling rather than setInterval: an interval does not wait for the
  // previous request, so a slow response landing after a fast later one would
  // repaint the caseload with the older snapshot.
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const data = await patients();
        if (!alive) return;
        setActive(data.active);
        setCompleted(data.completed);
        setError(null);
      } catch (err) {
        if (alive) setError(messageOf(err));
      } finally {
        if (alive) {
          setLoaded(true);
          timer = setTimeout(tick, 3000);
        }
      }
    };
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);

  const rows = [...active, ...completed];

  return (
    <Page>
      <Masthead facts={CORPUS_FACTS} />

      <h1 className="mt-8 text-3xl font-medium tracking-tight">Caseload</h1>

      {error ? <div className="mt-4"><Notice text={error} /></div> : null}

      {!loaded ? (
        // The route prerenders, so without this gate the static snapshot ships
        // hyphen tiles and "(0)" sections and every reload flashes an empty
        // dashboard before the first fetch lands.
        <p className="mt-10 text-sm text-muted">Loading caseload.</p>
      ) : (
        <>
          <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {tiles(rows).map((t) => (
              <div key={t.label} className="bg-surface p-5">
                <p className="text-xs uppercase tracking-wide text-muted">{t.label}</p>
                <p className="verbatim mt-3 text-4xl font-medium">{t.value}</p>
                <p className="mt-2 text-xs text-muted">{t.note}</p>
              </div>
            ))}
          </div>

          {/* Paired with the tile strip so the disclaimer is on the projector
              with the numbers it qualifies, not a scroll below them. */}
          <div className="mt-2 flex flex-wrap items-baseline justify-between gap-3">
            <p className="text-xs text-muted">across {rows.length} cases</p>
            <Disclaimer />
          </div>

          {rows.length === 0 && !error ? (
            <p className="mt-10 text-sm text-muted">No patients in the tracker.</p>
          ) : null}

          <Section title="In progress" cards={active} />
          <Section title="Completed" cards={completed} />
        </>
      )}

      <Disclaimer className="mt-12" />
    </Page>
  );
}
