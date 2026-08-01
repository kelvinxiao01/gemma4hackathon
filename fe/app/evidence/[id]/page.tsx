"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { evidence as fetchEvidence, messageOf } from "@/lib/client";
import type { Evidence } from "@/lib/types";
import { Notice, when } from "@/app/ui";

// A document, not an app screen. It is meant to be printed and handed over, so
// the layout stays plain and everything on it is quoted from the source.

export default function Receipt({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [doc, setDoc] = useState<Evidence | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchEvidence(id)
      .then((value) => {
        if (alive) setDoc(value);
      })
      .catch((err: unknown) => {
        if (alive) setError(messageOf(err));
      })
      .finally(() => {
        if (alive) setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [id]);

  if (!loaded) {
    return <main className="mx-auto max-w-3xl px-6 py-12 text-sm">Loading receipt.</main>;
  }

  if (!doc) {
    return (
      <main className="mx-auto max-w-3xl px-6 py-12">
        <h1 className="text-2xl font-medium">Receipt not found</h1>
        <p className="mt-3 text-sm text-muted">
          No evidence receipt with id <span className="verbatim">{id}</span>.
        </p>
        {error ? <div className="mt-4"><Notice text={error} /></div> : null}
        <Link href="/" className="mt-6 inline-block text-sm text-accent-text hover:underline">
          Back to the caseload
        </Link>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <nav className="screen-only mb-8 flex items-center justify-between text-sm">
        <Link href={"/patients/" + doc.patient_id} className="text-accent-text hover:underline">
          Back to {doc.patient_name}
        </Link>
        <Link href="/" className="text-accent-text hover:underline">
          caseload
        </Link>
      </nav>

      <header className="border-b border-rule pb-6">
        <p className="wordmark text-sm">CONDUIT EVIDENCE RECEIPT</p>
        <h1 className="mt-4 text-3xl font-medium tracking-tight">
          {doc.patient_name}
        </h1>
        <p className="mt-2 text-lg">{doc.determination}</p>
        <p className="verbatim mt-3 text-sm">
          Score {doc.score} of 100, band {doc.band}
        </p>
        <p className="verbatim mt-1 text-sm text-muted">
          Case {doc.patient_id}, generated {when(doc.created_at)}, model {doc.model}
        </p>
        {/* The footer copy is the one a printed page keeps. This one is for the
            screen, beside the determination it qualifies. */}
        <p className="mt-3 text-xs text-muted">{doc.disclaimer}</p>
      </header>

      <section className="mt-8">
        <h2 className="text-xs uppercase tracking-[0.18em] text-muted">
          Scoring rule
        </h2>
        <p className="mt-2 text-sm leading-relaxed">{doc.score_rule}</p>
      </section>

      <section className="mt-8">
        <h2 className="text-xs uppercase tracking-[0.18em] text-muted">
          Criteria
        </h2>
        <ol className="mt-4 space-y-6">
          {doc.criteria.map((c, i) => (
            <li key={c.criterion} className="keep-together border-t border-rule pt-4">
              <p className="font-medium">
                {i + 1}. {c.criterion}
              </p>
              <p className="verbatim mt-1 text-xs text-muted">
                {c.kind} / {c.status}
              </p>
              <p className="mt-2 text-sm">
                <span className="text-muted">Record: </span>
                {c.evidence}
              </p>
              <figure className="mt-3 border-l-2 border-rule pl-3">
                <blockquote className="text-sm italic">{c.citation.quote}</blockquote>
                <figcaption className="verbatim mt-2 text-xs">
                  {c.citation.payer_name} {c.citation.plan_type},{" "}
                  <span className="break-all">{c.citation.ref}</span>
                  {/* The shared source document is printed once, in the footer.
                      A ref that is itself the url has already printed it, so
                      repeating it here would say the same thing twice. */}
                  {c.citation.doc_url === doc.doc_url ||
                  c.citation.ref === c.citation.doc_url ? null : (
                    <>
                      <br />
                      <a href={c.citation.doc_url} className="break-all text-accent-text">
                        {c.citation.doc_url}
                      </a>
                    </>
                  )}
                </figcaption>
              </figure>
            </li>
          ))}
        </ol>
      </section>

      <section className="mt-10 border-t border-rule pt-6">
        <h2 className="text-xs uppercase tracking-[0.18em] text-muted">
          Source document
        </h2>
        <p className="verbatim mt-2 break-all text-xs">{doc.doc_url}</p>
        <p className="verbatim mt-1 break-all text-xs">sha256 {doc.doc_sha256}</p>
      </section>

      <footer className="mt-10 border-t border-rule pt-6">
        <p className="text-sm">{doc.disclaimer}</p>
      </footer>
    </main>
  );
}
