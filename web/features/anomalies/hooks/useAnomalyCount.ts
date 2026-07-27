"use client";

import { useQuery } from "@tanstack/react-query";

import { fetchAnomalyCount } from "@/features/anomalies/api/flaggedWindow";
import { STALE_TIME_MS } from "@/lib/query";
import { useScope } from "@/lib/useScope";

export interface AnomalyCountState {
  /** Exact flagged-row count; null until the first fetch settles. */
  readonly count: number | null;
  /** True during the initial in-flight fetch (not refetches). */
  readonly isLoading: boolean;
  readonly isError: boolean;
  /** The thrown Error when isError is true; null otherwise. */
  readonly error: Error | null;
}

/**
 * Cheaply resolves the exact flagged-row count for a window.
 * Fetches /readings/anomalies-in-window with max_points=1 so only one row is
 * transferred — the response always includes the full anomaly_count.
 *
 * Use this instead of deriving the count from the slow G4 enriched endpoint,
 * which takes 40+ seconds in production and causes the grouping decision to
 * temporarily use the downsampled chart row count (undercounting by ~70×).
 */
export function useAnomalyCount(params: {
  windowStart: string | null;
  windowEnd: string | null;
  threshold: number;
  enabled: boolean;
}): AnomalyCountState {
  const { tenant, gh } = useScope();
  const { windowStart, windowEnd, threshold, enabled } = params;

  const query = useQuery<number, Error>({
    queryKey: ["anomaly-count", tenant, gh, windowStart, windowEnd, threshold],
    queryFn: () =>
      fetchAnomalyCount({
        tenant,
        gh,
        start: windowStart ?? "",
        end: windowEnd ?? "",
        threshold,
      }),
    enabled: enabled && windowStart !== null && windowEnd !== null,
    staleTime: STALE_TIME_MS.anomaliesInWindow,
    retry: 1,
  });

  return {
    count: query.data ?? null,
    isLoading: query.isPending && query.isFetching,
    isError: query.isError,
    error: query.error,
  };
}
