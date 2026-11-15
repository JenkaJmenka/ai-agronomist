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
