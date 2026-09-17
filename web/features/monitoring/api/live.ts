/**
 * Zod schemas and TS types for the live monitoring SSE stream.
 *
 * Field shapes mirror the Pydantic v2 models in src/live/events.py.
 * Loose parsing (`.passthrough()` where appropriate) lets the frontend
 * survive additive backend changes without schema errors.
 */

import { z } from "zod";

import type { SensorRow } from "@/features/monitoring/api/readings";

// ---------------------------------------------------------------------------
// reading
// ---------------------------------------------------------------------------

export const liveReadingEventSchema = z
  .object({
    /** Always "" in SSE payloads — authoritative id is the SSE `id:` field. */
    seq: z.string(),
    timestamp: z.string(),
    tenant_id: z.string(),
    greenhouse_id: z.string(),
    /** channel name → numeric value */
    channels: z.record(z.string(), z.number()),
    /** channel name → source_id that provided the value */
    sources: z.record(z.string(), z.string()),
    /** null until the 20-reading scoring window warms up */
    anomaly_score: z.number().nullable().optional(),
  })
  .passthrough();

export type LiveReadingEvent = z.infer<typeof liveReadingEventSchema>;

// ---------------------------------------------------------------------------
// anomaly
// ---------------------------------------------------------------------------

export const liveAnomalyEventSchema = z
  .object({
    seq: z.string(),
    timestamp: z.string(),
    tenant_id: z.string(),
    greenhouse_id: z.string(),
    score: z.number(),
    threshold: z.number(),
    affected_channels: z.array(z.string()),
    reasoning: z.string(),
    window_start: z.string(),
    window_end: z.string(),
  })
  .passthrough();

export type LiveAnomalyEvent = z.infer<typeof liveAnomalyEventSchema>;

// ---------------------------------------------------------------------------
// anomaly-detail  (enriches a previously emitted anomaly entry in place)
// ---------------------------------------------------------------------------

export const anomalyDetailEventSchema = z
  .object({
    anomaly_seq: z.string(),
    tenant_id: z.string(),
    greenhouse_id: z.string(),
    reasoning: z.string(),
    fault_type: z.string().nullable().optional(),
    root_cause: z.string().nullable().optional(),
    /** UUID of the linked Incident record; null until the enrichment pipeline
     *  resolves or when no incident was created. String type matches the UUID
     *  PK on the Incident model (contract 3). */
    incident_id: z.string().nullable().optional(),
  })
  .passthrough();

export type AnomalyDetailEvent = z.infer<typeof anomalyDetailEventSchema>;

// ---------------------------------------------------------------------------
// source-status
// ---------------------------------------------------------------------------

export const sourceStatusEventSchema = z
  .object({
    source_id: z.string(),
    vendor: z.string(),
    status: z.enum(["online", "degraded", "offline"]),
    last_seen: z.string().nullable().optional(),
  })
  .passthrough();

export type SourceStatus = z.infer<typeof sourceStatusEventSchema>;

// ---------------------------------------------------------------------------
// heartbeat  (minimal — keeps the stale timer alive; payload may grow)
// ---------------------------------------------------------------------------

export const heartbeatEventSchema = z
  .object({
    /** Backend sends `{"ts": "<iso>"}` (routes/live.py:43). Field is optional
     *  because the stale timer resets on arrival, not on reading the value. */
    ts: z.string().optional(),
  })
  .passthrough();

export type HeartbeatEvent = z.infer<typeof heartbeatEventSchema>;

// ---------------------------------------------------------------------------
// reset  (signals the stream is resetting; clear local state)
// ---------------------------------------------------------------------------

export const resetEventSchema = z
  .object({
    reason: z.string().optional(),
  })
  .passthrough();

export type ResetEvent = z.infer<typeof resetEventSchema>;

// ---------------------------------------------------------------------------
// GET /api/bff/live/status response  (CONTRACT 2)
// ---------------------------------------------------------------------------

/**
 * Shape returned by `GET /api/live/status`.
 *
 * Contract 2: `{ sources: [{ source_id, vendor, status, last_seen? }] }`.
 * `last_seen` is nullable/optional so the schema stays tolerant across backend
 * deploy windows where the field may temporarily be absent (N6 fix).
 */
export const statusResponseSchema = z.object({
  sources: z.array(sourceStatusEventSchema),
});

export type StatusResponse = z.infer<typeof statusResponseSchema>;

// ---------------------------------------------------------------------------
// POST /api/bff/live/{resume,pause}  (live scope control)
// ---------------------------------------------------------------------------

/**
 * `scope_status` is optional: an older API deployed ahead of the frontend
 * during a rolling deploy window won't send it yet (deploy-skew history —
 * see project memory). Treat its absence as "no new information", not
 * failure.
 */
const resumeResponseSchema = z.object({
  resumed: z.boolean(),
  scope_status: z.enum(["started", "already_running", "not_configured"]).optional(),
  tenant_id: z.string(),
  greenhouse_id: z.string(),
});

export type ResumeLiveScopeResult = z.infer<typeof resumeResponseSchema>;

const pauseResponseSchema = z.object({
  paused: z.boolean(),
  scope_status: z.enum(["stopped", "not_running", "not_configured"]).optional(),
  tenant_id: z.string(),
  greenhouse_id: z.string(),
});

/** POST the tenant/gh scope to a live-control action and zod-parse the response. */
async function postLiveScopeAction<T>(
  action: "resume" | "pause",
  params: { tenant: string; gh: string },
  schema: z.ZodType<T>,
): Promise<T> {
  const query = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
  });
  const res = await fetch(`/api/bff/live/${action}?${query}`, { method: "POST" });
  if (!res.ok) {
    throw new Error(`Failed to ${action} live scope (${res.status})`);
  }
  return schema.parse(await res.json());
}

/**
 * Re-enable live publish/persistence after Clear all data muted the scope.
 * Called when the operator turns Go Live on. Returns the parsed result so
 * callers can react to `scope_status: "not_configured"` — an HTTP 200 that
 * will never actually stream because the greenhouse has no configured live
 * source.
 */
export async function resumeLiveScope(params: {
  tenant: string;
  gh: string;
}): Promise<ResumeLiveScopeResult> {
  return postLiveScopeAction("resume", params, resumeResponseSchema);
}

/**
 * Mute live publish/persistence for a scope. Live scopes now start muted on
 * the backend, so this is called when the operator turns Go Live off, to
 * stop synthetic mock-SCADA rows from continuing to land in the DB behind
 * the UI's back.
 */
export async function pauseLiveScope(params: { tenant: string; gh: string }): Promise<void> {
  await postLiveScopeAction("pause", params, pauseResponseSchema);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Flatten a `LiveReadingEvent` into the `SensorRow` shape that the chart,
 * table, and `discoverChannels` already consume.
 * `channels` entries are spread to top-level keys; `timestamp` and
 * `anomaly_score` stay at the top level.
 */
export function toSensorRow(event: LiveReadingEvent): SensorRow {
  return {
    timestamp: event.timestamp,
    anomaly_score: event.anomaly_score ?? null,
    ...event.channels,
  };
}
