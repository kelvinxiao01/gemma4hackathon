// The only place the frontend talks to anything. NEXT_PUBLIC_LIVE picks the
// real backend; everything else runs against lib/mock.ts so the demo is
// rehearsable with no server. Env values are inlined at build time, so the
// literal process.env.NEXT_PUBLIC_X form is required and a restart is needed to
// change modes.

import type {
  Card,
  Evidence,
  Health,
  PatientDetail,
  Stage,
} from "./types";
import {
  type Conflict,
  mockEvent,
  mockEvidence,
  mockHealth,
  mockPatient,
  mockPatients,
  mockQualify,
  mockReset,
  mockSchedule,
  mockSubmit,
} from "./mock";

export type { Conflict } from "./mock";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
export const LIVE = process.env.NEXT_PUBLIC_LIVE === "1";

/** Defense only. The backend keeps the sentinel and the JSON after it. */
const SENTINEL = "===RESULT===";

/** A browser reports the same "Failed to fetch" for a dead server and for a
 *  missing CORS header, and those have different fixes. Name the cause. */
async function send(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(BASE + path, init);
  } catch (err) {
    // A caller-supplied abort reason is whatever the caller passed, so check the
    // signal before the DOMException shape: a deliberate cancellation must not
    // be reported as a dead server.
    if (init?.signal?.aborted) throw err;
    if (
      err instanceof DOMException &&
      (err.name === "AbortError" || err.name === "TimeoutError")
    ) {
      throw err;
    }
    throw new Error(
      "Cannot reach " +
        BASE +
        ": server down, or CORS blocked. Check the backend terminal.",
      // The original carries the per-browser text that separates a dead port
      // from a CORS preflight, which is the whole reason this branch exists.
      { cause: err },
    );
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await send(path);
  if (!res.ok) throw new Error(path + " returned " + res.status);
  return res.json();
}

/** 404 is a routine answer here, not an exception: an unknown id is a page. */
async function getOrNull<T>(path: string): Promise<T | null> {
  const res = await send(path);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(path + " returned " + res.status);
  return res.json();
}

/** A 409 is a state the user can read and act on, so it becomes an inline
 *  message rather than an exception thrown at an error boundary. */
async function conflictOf(res: Response): Promise<Conflict> {
  const body = await res.text().catch(() => "");
  let detail = body.trim();
  try {
    const parsed: unknown = JSON.parse(body);
    if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
      if (typeof parsed.detail === "string") detail = parsed.detail;
    }
  } catch {
    // Not JSON. The raw body is the best message available.
  }
  return {
    ok: false,
    message: detail.slice(0, 300) || "That action is not available yet.",
  };
}

async function post(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<Response> {
  return send(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
}

export async function patients(): Promise<{ active: Card[]; completed: Card[] }> {
  if (!LIVE) return mockPatients();
  return getJson("/copilot/patients");
}

export async function patient(id: string): Promise<PatientDetail | null> {
  if (!LIVE) return mockPatient(id);
  return getOrNull("/copilot/patients/" + encodeURIComponent(id));
}

export async function evidence(id: string): Promise<Evidence | null> {
  if (!LIVE) return mockEvidence(id);
  return getOrNull("/copilot/evidence/" + encodeURIComponent(id));
}

export async function health(): Promise<Health> {
  if (!LIVE) return mockHealth();
  return getJson("/copilot/health");
}

export async function reset(): Promise<{ ok: true }> {
  if (!LIVE) return mockReset();
  const res = await post("/copilot/reset");
  if (!res.ok) throw new Error("reset returned " + res.status);
  return { ok: true };
}

/** Appends an event to a patient's timeline. On the contract, so it is here;
 *  the backend writes its own events, so nothing in the UI calls it yet. */
export async function event(
  id: string,
  kind: string,
  message: string,
  payload?: Record<string, unknown>,
): Promise<{ ok: true } | Conflict> {
  if (!LIVE) return mockEvent(id, kind, message, payload);
  const res = await post(
    "/copilot/patients/" + encodeURIComponent(id) + "/events",
    { kind, message, payload },
  );
  if (res.status === 409) return conflictOf(res);
  if (!res.ok) throw new Error("events returned " + res.status);
  return { ok: true };
}

export async function submit(
  id: string,
  action: "submit" | "do_not_submit",
): Promise<{ ok: true; stage: Stage } | Conflict> {
  if (!LIVE) return mockSubmit(id, action);
  const res = await post("/copilot/patients/" + encodeURIComponent(id) + "/submit", {
    action,
  });
  if (res.status === 409) return conflictOf(res);
  if (!res.ok) throw new Error("submit returned " + res.status);
  return res.json();
}

export async function schedule(id: string): Promise<{ ok: true } | Conflict> {
  if (!LIVE) return mockSchedule(id);
  const res = await post("/copilot/patients/" + encodeURIComponent(id) + "/schedule");
  if (res.status === 409) return conflictOf(res);
  if (!res.ok) throw new Error("schedule returned " + res.status);
  return res.json();
}

/**
 * Streams the qualification reasoning. Resolves when the stream closes; the
 * caller then refetches the patient for the structured result.
 */
export async function qualify(
  id: string,
  onToken: (text: string) => void,
  signal?: AbortSignal,
): Promise<{ ok: true } | Conflict> {
  if (!LIVE) return mockQualify(id, onToken, signal);

  // The signal has to reach fetch, not just the reader: cancelling the reader
  // closes our end, but only aborting the request reliably stops the backend
  // generating, and concurrency on the demo machine is 1.
  const res = await post(
    "/copilot/patients/" + encodeURIComponent(id) + "/qualify",
    undefined,
    signal,
  );
  if (res.status === 409) return conflictOf(res);
  if (!res.ok) throw new Error("qualify returned " + res.status);
  if (!res.body) throw new Error("qualify returned no stream body");

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let emitted = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // Abort mid-body still has to leave through the finally below.
      if (signal?.aborted) break;
      if (!value) continue;
      buffer += value;
      const cut = buffer.indexOf(SENTINEL);
      if (cut >= 0) {
        if (cut > emitted) onToken(buffer.slice(emitted, cut));
        return { ok: true };
      }
      // Hold back enough tail to catch a sentinel split across two chunks.
      const safe = buffer.length - (SENTINEL.length - 1);
      if (safe > emitted) {
        onToken(buffer.slice(emitted, safe));
        emitted = safe;
      }
    }
    if (buffer.length > emitted) onToken(buffer.slice(emitted));
  } finally {
    // Without this, navigating away mid-stream leaks the reader and leaves the
    // backend generating tokens nobody reads.
    await reader.cancel().catch(() => {});
  }
  return { ok: true };
}

/** Turns anything thrown by this module into a line worth showing a user. */
export function messageOf(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}
