/**
 * Zod schemas and TS types for the live monitoring SSE stream.
 *
 * Field shapes mirror the Pydantic v2 models in src/live/events.py.
 * Loose parsing (`.passthrough()` where appropriate) lets the frontend
 * survive additive backend changes without schema errors.
 */

import { z } from "zod";

import type { SensorRow } from "@/features/monitoring/api/readings";
import type { components } from "@/lib/api-types.gen";

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
// GET /api/bff/live/status response  (CONTRACT 2 + freshness)
// ---------------------------------------------------------------------------

/**
 * The response shapes as FastAPI actually declares them, pulled from the
 * committed OpenAPI snapshot. `openapi-drift.yml` rebuilds `api-types.gen.ts`
 * from `src/` on every PR and fails on any difference, so these aliases make
 * the backend contract a compile-time input to the Zod schemas below rather
 * than something a human has to keep in step by hand.
 */
type GeneratedFreshness = components["schemas"]["LiveFreshness"];
type GeneratedStatusResponse = components["schemas"]["LiveStatusResponse"];

/** Mutual assignability: `never` (and so a type error at the use site) if A and B differ. */
type IsExactly<A, B> = [A] extends [B] ? ([B] extends [A] ? true : never) : never;

/**
 * Connector-only verdict for a scope (narrower than `greenhouse_status`,
 * which also covers registry states like `not_registered`/`no_source` that
 * say nothing about liveness). See `src/api/services/live_freshness.py`.
 */
export const FRESHNESS_STATE_VALUES = [
  "live",
  "paused",
  "not_configured",
] as const satisfies readonly GeneratedFreshness["state"][];
export type FreshnessState = (typeof FRESHNESS_STATE_VALUES)[number];

export const GREENHOUSE_STATUS_VALUES = [
  "not_registered",
  "no_source",
  "live",
  "paused",
] as const satisfies readonly GeneratedStatusResponse["greenhouse_status"][];
export type GreenhouseStatus = (typeof GREENHOUSE_STATUS_VALUES)[number];

/**
 * Is data actually arriving for this scope — distinct from "is Go Live on".
 * A production audit found zero `source='live'` rows ever written for any
 * tenant while the scope-control toggle happily reported "live", so this
 * block answers the question that actually matters to an operator.
 *
 * `age_seconds` is never clamped: readings are routinely future-dated here,
 * so a negative age is a normal, meaningful value, not an error state.
 * `is_stale` is true only for a `live` scope, including one that has never
 * written a row at all (`newest_reading_at: null`) — a paused/unconfigured
 * scope is off, not broken.
 */
export const freshnessSchema = z.object({
  state: z.enum(FRESHNESS_STATE_VALUES),
  newest_reading_at: z.string().nullable(),
  age_seconds: z.number().nullable(),
  is_stale: z.boolean(),
  threshold_seconds: z.number(),
});

export type Freshness = z.infer<typeof freshnessSchema>;

/**
 * Compile-time proof that `freshnessSchema` and the generated OpenAPI type
 * describe the *same* object. Drift in either direction — a field added,
 * removed, renamed, or retyped on the Pydantic model — makes `IsExactly`
 * resolve to `never` and fails `pnpm typecheck`.
 *
 * The schema stays hand-written rather than being replaced outright by the
 * generated type because the runtime still needs a parser: the generated file
 * is types-only, and this response crosses the BFF proxy, so it is untrusted
 * input that must be validated at the boundary. This assertion gives the
 * generated type the final say over its shape without giving up the parse.
 */
export const FRESHNESS_SCHEMA_MATCHES_OPENAPI: IsExactly<Freshness, GeneratedFreshness> = true;

/**
 * Shape returned by `GET /api/live/status`.
 *
 * Contract 2: `{ sources: [{ source_id, vendor, status, last_seen? }] }`.
 * `last_seen` is nullable/optional so the schema stays tolerant across backend
 * deploy windows where the field may temporarily be absent (N6 fix).
 *
 * `greenhouse_status` and `freshness` are both optional for the same
 * deploy-skew reason as `scope_status` below: an older API instance mid
 * rolling-deploy won't send them yet. Treat their absence as "no new
 * information" — never as "idle" or "not stale".
 */
export const statusResponseSchema = z.object({
  greenhouse_status: z.enum(GREENHOUSE_STATUS_VALUES).optional(),
  sources: z.array(sourceStatusEventSchema),
  freshness: freshnessSchema.optional(),
});

export type StatusResponse = z.infer<typeof statusResponseSchema>;

/**
 * One-directional on purpose. `statusResponseSchema` cannot be an exact match
 * for the generated type, and should not be: `greenhouse_status`/`freshness`
 * are optional and `sources` is `.passthrough()` specifically so a rolling
 * deploy where the API is older than this bundle still parses. That tolerance
 * is a deliberate superset of the contract, so exact equality would be the
 * wrong assertion.
 *
 * What is still worth proving is the direction that matters: anything the API
 * can legitimately send must be accepted. If the backend adds a required field
 * or narrows an existing one incompatibly, this fails `pnpm typecheck` instead
 * of surfacing as a runtime `.parse()` throw in the dashboard.
 */
export const STATUS_SCHEMA_ACCEPTS_OPENAPI: GeneratedStatusResponse extends z.input<
  typeof statusResponseSchema
>
  ? true
  : never = true;

/** Fetch + zod-parse `GET /api/bff/live/status` for one tenant/greenhouse scope. */
export async function fetchLiveStatus(params: {
  tenant: string;
  gh: string;
}): Promise<StatusResponse> {
  const query = new URLSearchParams({ tenant_id: params.tenant, greenhouse_id: params.gh });
  const res = await fetch(`/api/bff/live/status?${query}`);
  if (!res.ok) {
    throw new Error(`Live status request failed (${res.status})`);
  }
  return statusResponseSchema.parse(await res.json());
}

// ---------------------------------------------------------------------------
// POST /api/bff/live/{resume,pause}  (live scope control)
// ---------------------------------------------------------------------------

/**
 * `scope_status` is optional: an older API deployed ahead of the frontend
 * during a rolling deploy window won't send it yet (deploy-skew history —
 * see project memory). Treat its absence as "no new information", not
 * failure.
 *
 * "unmanaged" is the runtime-unavailable case: when app.state.live_runtime
 * is None the route has no runtime to ask and no way to tell started/
 * already_running/not_configured apart, so it says so rather than
 * guessing. It is NOT a "not configured" verdict — the toggle must treat
 * it as indeterminate, not act on it.
 */
const resumeResponseSchema = z.object({
  resumed: z.boolean(),
  scope_status: z.enum(["started", "already_running", "not_configured", "unmanaged"]).optional(),
  tenant_id: z.string(),
  greenhouse_id: z.string(),
});

export type ResumeLiveScopeResult = z.infer<typeof resumeResponseSchema>;

const pauseResponseSchema = z.object({
  paused: z.boolean(),
  scope_status: z.enum(["stopped", "not_running", "not_configured", "unmanaged"]).optional(),
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
