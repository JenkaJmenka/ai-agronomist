/** Drift watch data access via the BFF. */

import { z } from "zod";

export const driftWatchInputSchema = z
  .object({
    input_id: z.string(),
    kind: z.string(),
    target_channel: z.string(),
    status: z.string(),
    status_reason: z.string(),
    calibrated_at: z.string().nullable().optional(),
    updated_at: z.string(),
    false_alarm_rate: z.number().nullable().optional(),
    detection_rate: z.number().nullable().optional(),
    greenhouse_id: z.string().optional(),
    alert_state: z.string().optional(),
    alert_state_reason: z.string().nullable().optional(),
    oos_count: z.number().nullable().optional(),
    raw_count: z.number().nullable().optional(),
    budget: z.number().optional(),
  })
  .loose();

export type DriftWatchInput = z.infer<typeof driftWatchInputSchema>;

export const driftWatchEpisodeSchema = z
  .object({
    input_id: z.string(),
    episode_id: z.string(),
    target_channel: z.string(),
    direction: z.string(),
    first_detected_ts: z.string(),
    last_confirmed_ts: z.string(),
    onset_estimate_ts: z.string().nullable().optional(),
    offset_estimate: z.number().nullable().optional(),
    peak_statistic: z.number().nullable().optional(),
    input_status_at_detection: z.string(),
    ongoing: z.boolean(),
    armed: z.boolean(),
    offset_robust: z.number().nullable().optional(),
    suppressed_by: z.string().nullable().optional(),
    label: z.string().nullable().optional(),
    alert_message: z.string().nullable().optional(),
  })
  .loose();

export type DriftWatchEpisode = z.infer<typeof driftWatchEpisodeSchema>;

const inputListSchema = z.array(driftWatchInputSchema);
const episodeListSchema = z.array(driftWatchEpisodeSchema);

export async function fetchDriftWatchInputs(params: {
  tenant: string;
  gh: string;
}): Promise<DriftWatchInput[]> {
  const query = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
  });
  const res = await fetch(`/api/bff/drift-watch/inputs?${query}`);
  if (!res.ok) {
    throw new Error(`Drift watch inputs request failed (${res.status})`);
  }
  return inputListSchema.parse(await res.json());
}

export async function fetchDriftWatchEpisodes(params: {
  tenant: string;
  gh: string;
  limit?: number;
}): Promise<DriftWatchEpisode[]> {
  const query = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
    limit: String(params.limit ?? 50),
  });
  const res = await fetch(`/api/bff/drift-watch/episodes?${query}`);
  if (!res.ok) {
    throw new Error(`Drift watch episodes request failed (${res.status})`);
  }
  return episodeListSchema.parse(await res.json());
}

export type DriftWatchLabel = "confirmed_drift" | "false_alarm";

/** Natural key of an episode plus the operator's label (matches DriftWatchFeedbackRequest). */
export interface DriftWatchFeedbackInput {
  readonly tenant_id: string;
  readonly greenhouse_id: string;
  readonly input_id: string;
  readonly direction: "+" | "-";
  readonly first_detected_ts: string;
  readonly label: DriftWatchLabel;
  readonly note?: string;
  readonly created_by?: string;
}

export async function postDriftWatchFeedback(body: DriftWatchFeedbackInput): Promise<void> {
  const res = await fetch("/api/bff/drift-watch/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`Drift watch feedback request failed (${res.status})`);
  }
}

export const driftSeriesPointSchema = z
  .object({
    ts: z.string(),
    target: z.number().nullable().optional(),
    reference: z.number().nullable().optional(),
    residual: z.number().nullable().optional(),
    dev: z.number().nullable().optional(),
    s_pos: z.number().nullable().optional(),
    s_neg: z.number().nullable().optional(),
    alarm_dir: z.string().nullable().optional(),
  })
  .loose();

export const driftSeriesEpisodeSchema = z
  .object({
    first_detected_ts: z.string(),
    onset_ts: z.string().nullable().optional(),
    last_confirmed_ts: z.string().nullable().optional(),
    direction: z.string(),
    offset_robust: z.number().nullable().optional(),
    label: z.string().nullable().optional(),
    suppressed_by: z.string().nullable().optional(),
  })
  .loose();

export const driftSeriesSchema = z
  .object({
    input_id: z.string(),
    kind: z.string(),
    target_channel: z.string(),
    unit: z.string(),
    status: z.string(),
    alert_state: z.string(),
    calibration_window_end: z.string().nullable().optional(),
    min_shift: z.number(),
    alert_floor: z.number(),
    h: z.number().nullable().optional(),
    sigma: z.number().nullable().optional(),
    window_start: z.string(),
    window_end: z.string(),
    points: z.array(driftSeriesPointSchema),
    episodes: z.array(driftSeriesEpisodeSchema),
    replay_mismatch: z.number(),
    reason: z.string().nullable().optional(),
  })
  .loose();

export type DriftSeries = z.infer<typeof driftSeriesSchema>;
export type DriftSeriesPoint = z.infer<typeof driftSeriesPointSchema>;
export type DriftSeriesEpisode = z.infer<typeof driftSeriesEpisodeSchema>;

export const DRIFT_WINDOW_WEEKS = [13, 26, 52] as const;
export type DriftWindowWeeks = (typeof DRIFT_WINDOW_WEEKS)[number];

const MS_PER_WEEK = 7 * 24 * 3600 * 1000;
const MS_PER_HOUR = 3600 * 1000;

/** Replayed series for one input over the last `weeks` weeks (end floored to the hour). */
export async function fetchDriftWatchSeries(params: {
  tenant: string;
  gh: string;
  inputId: string;
  weeks: number;
}): Promise<DriftSeries> {
  const endMs = Math.floor(Date.now() / MS_PER_HOUR) * MS_PER_HOUR;
  const query = new URLSearchParams({
    start: new Date(endMs - params.weeks * MS_PER_WEEK).toISOString(),
    end: new Date(endMs).toISOString(),
  });
  const path = [params.tenant, params.gh, params.inputId].map(encodeURIComponent).join("/");
  const res = await fetch(`/api/bff/drift-watch/inputs/${path}/series?${query}`);
  if (!res.ok) {
    throw new Error(`Drift watch series request failed (${res.status})`);
  }
  return driftSeriesSchema.parse(await res.json());
}
