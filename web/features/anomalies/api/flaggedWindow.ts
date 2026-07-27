/** G2 exact flagged-rows endpoint via the BFF — the anomaly-count source of
 * truth. Chart rows are LTTB-downsampled and drop flagged rows unevenly per
 * window, so anything counting or grouping anomalies must read this instead. */

import { z } from "zod";

import { type AnomalyWindowParams, windowQuery } from "@/features/anomalies/api/enriched";
import type { SensorRow } from "@/features/monitoring/api/readings";

/**
 * Mirrors DOWNSAMPLE_MAX_POINTS_FLOOR in src/config.py (default 100).
 * The backend declares max_points with ge=DOWNSAMPLE_MAX_POINTS_FLOOR, so any
 * request sending a lower value gets a 422. Keep this in sync if the Python
 * floor ever changes.
 */
export const DOWNSAMPLE_MAX_POINTS_FLOOR = 100;

/** Above this many flagged rows, grouping moves server-side (G3) and the
 * flagged-rows payload is capped at this many points. */
export const SERVER_GROUPING_THRESHOLD = 500;

const flaggedRowSchema = z
  .object({
    timestamp: z.string(),
    anomaly_score: z.number().nullable().optional(),
  })
  .loose();

const flaggedResponseSchema = z
  .object({
    rows: z.array(flaggedRowSchema),
    anomaly_count: z.number(),
    total_count: z.number(),
  })
  .loose();

export interface FlaggedWindowResult {
  /** Threshold-filtered rows for the full window (complete when
   * anomalyCount ≤ SERVER_GROUPING_THRESHOLD, downsampled above it). */
  readonly rows: SensorRow[];
  /** Flagged count before any downsampling. */
  readonly anomalyCount: number;
}

export async function fetchFlaggedWindow(
  params: AnomalyWindowParams,
): Promise<FlaggedWindowResult> {
  const query = windowQuery(params);
  query.set("max_points", String(SERVER_GROUPING_THRESHOLD));
  const res = await fetch(`/api/bff/readings/anomalies-in-window?${query}`);
  if (!res.ok) {
    throw new Error(`Flagged window request failed (${res.status})`);
  }
  const body = flaggedResponseSchema.parse(await res.json());
  return { rows: body.rows, anomalyCount: body.anomaly_count };
}

/**
 * Cheap count-only variant: sends max_points at the backend floor so the
 * minimum number of rows are transferred. The response always carries the full
 * exact anomaly_count from a SQL COUNT independent of max_points, so this is
 * far faster than fetching the full row payload.
 *
 * NOTE: must not send max_points below DOWNSAMPLE_MAX_POINTS_FLOOR — the
 * backend rejects lower values with HTTP 422 (ge constraint).
 */
export async function fetchAnomalyCount(params: AnomalyWindowParams): Promise<number> {
  const query = windowQuery(params);
  query.set("max_points", String(DOWNSAMPLE_MAX_POINTS_FLOOR));
  const res = await fetch(`/api/bff/readings/anomalies-in-window?${query}`);
  if (!res.ok) {
    throw new Error(`Anomaly count request failed (${res.status})`);
  }
  const body = flaggedResponseSchema.parse(await res.json());
  return body.anomaly_count;
}
