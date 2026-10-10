/** Query-time anomaly episodes — contiguous flagged samples merged into events.
 *
 * The flat tables show one row per 5-minute sample, so a single continuous
 * event appears as N near-identical rows. This endpoint returns each event
 * once, with its span, extremum and trend direction.
 */

import { z } from "zod";

import { type AnomalyWindowParams, windowQuery } from "@/features/anomalies/api/enriched";

export const EPISODE_DIRECTIONS = ["drop", "spike"] as const;
export type EpisodeDirection = (typeof EPISODE_DIRECTIONS)[number];

export const EPISODE_TRENDS = ["worsening", "easing", "flat"] as const;
export type EpisodeTrend = (typeof EPISODE_TRENDS)[number];

export const EPISODE_MOVEMENTS = ["rising", "falling", "steady"] as const;
export type EpisodeMovement = (typeof EPISODE_MOVEMENTS)[number];

/**
 * Mirrors ANOMALY_EPISODE_MAX in src/config.py (default 200). The backend
 * ranks episodes by peak_score and caps the returned list at this many —
 * episode_count already reflects the capped length, so an episode list at
 * exactly this length is the only client-side signal that more episodes
 * exist in the window than are shown.
 */
export const ANOMALY_EPISODE_MAX = 200;

const episodeSampleSchema = z.object({
  timestamp: z.string(),
  anomaly_score: z.number().nullable().optional(),
  affected_sensors: z.array(z.string()).default([]),
  reasoning: z.string().nullable().optional(),
  values: z.record(z.string(), z.number()).default({}),
  detected_severity: z.string().nullable().optional(),
});

const episodeSchema = z.object({
  episode_id: z.string(),
  channel: z.string(),
  detector: z.string(),
  direction: z.enum(EPISODE_DIRECTIONS).nullable(),
  start_ts: z.string(),
  end_ts: z.string(),
  duration_seconds: z.number(),
  sample_count: z.number(),
  peak_score: z.number(),
  peak_ts: z.string(),
  severity: z.string(),
  first_value: z.number().nullable(),
  last_value: z.number().nullable(),
  /** The extremum reached during the episode — min for a drop, max for a spike. */
  peak_value: z.number().nullable(),
  /** Band-relative severity direction; "flat" whenever `direction` is null. */
  trend: z.enum(EPISODE_TRENDS),
  /** Which way the value actually moved — available on every channel. */
  movement: z.enum(EPISODE_MOVEMENTS).default("steady"),
  slope_per_hour: z.number().nullable(),
  affected_sensors: z.array(z.string()).default([]),
  headline: z.string(),
  /** True when the queried window clipped the episode — duration is a lower bound. */
  is_truncated_start: z.boolean(),
  is_truncated_end: z.boolean(),
  samples: z.array(episodeSampleSchema).default([]),
});

export type AnomalyEpisode = z.infer<typeof episodeSchema>;
export type AnomalyEpisodeSample = z.infer<typeof episodeSampleSchema>;

const episodesResponseSchema = z
  .object({
    episodes: z.array(episodeSchema),
    episode_count: z.number(),
    anomaly_count: z.number(),
  })
  .loose();

export interface AnomalyEpisodesResult {
  readonly episodes: AnomalyEpisode[];
  readonly episodeCount: number;
  /** Full-window flagged row count — the samples these episodes merge. */
  readonly anomalyCount: number;
}

export async function fetchAnomalyEpisodes(
  params: AnomalyWindowParams & { signal?: AbortSignal },
): Promise<AnomalyEpisodesResult> {
  const query = windowQuery(params);
  const res = await fetch(`/api/bff/anomalies/episodes?${query}`, {
    signal: params.signal,
  });
  if (!res.ok) {
    throw new Error(`Anomaly episodes request failed (${res.status})`);
  }
  const body = episodesResponseSchema.parse(await res.json());
  return {
    episodes: body.episodes,
    episodeCount: body.episode_count,
    anomalyCount: body.anomaly_count,
  };
}
