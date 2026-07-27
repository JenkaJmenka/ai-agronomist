import { SERVER_GROUPING_THRESHOLD } from "@/features/anomalies/api/flaggedWindow";

/** Rendering mode for the grouped anomaly table. */
export type GroupingMode = "server" | "client" | "loading";

/**
 * Derives the grouping mode from an exact flagged-row count.
 *
 * Returns `"loading"` when the count is still null (fetch in flight or not
 * yet started) so callers never accidentally commit to either rendering path
 * before the count is known — which would silently corrupt the enrichment
 * lookup in the client path (row subset mismatch) or omit server groups.
 */
export function resolveGroupingMode(count: number | null): GroupingMode {
  if (count === null) return "loading";
  return count >= SERVER_GROUPING_THRESHOLD ? "server" : "client";
}
