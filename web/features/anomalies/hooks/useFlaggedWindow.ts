"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import {
  type FlaggedWindowResult,
  fetchFlaggedWindow,
} from "@/features/anomalies/api/flaggedWindow";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import { STALE_TIME_MS } from "@/lib/query";
import { getSeverity } from "@/lib/severity";
import { useScope } from "@/lib/useScope";

export interface FlaggedWindowState {
  /** Exact flagged rows of the full window, most severe first. */
  readonly anomalies: AnomalyRow[];
  readonly isLoading: boolean;
  /** Threshold/window changed; stale rows stay on screen while loading. */
  readonly isRefetching: boolean;
  readonly isError: boolean;
  /** The thrown Error when isError is true; null otherwise. */
  readonly error: Error | null;
}

/**
 * Exact anomaly rows for the window (G2). Unlike useAnomalies — which flags
 * the downsampled chart rows and therefore undercounts unevenly per range —
 * this reads the server's threshold-filtered rows, so grouping and totals
 * stay consistent when switching date ranges.
 */
export function useFlaggedWindow(params: {
  windowStart: string | null;
  windowEnd: string | null;
  threshold: number;
  enabled: boolean;
}): FlaggedWindowState {
  const { tenant, gh } = useScope();
  const { windowStart, windowEnd, threshold, enabled } = params;

  const query = useQuery<FlaggedWindowResult, Error>({
    queryKey: ["flagged-window", tenant, gh, windowStart, windowEnd, threshold],
    queryFn: () =>
      fetchFlaggedWindow({
        tenant,
        gh,
        start: windowStart ?? "",
        end: windowEnd ?? "",
        threshold,
      }),
    enabled: enabled && windowStart !== null && windowEnd !== null,
    staleTime: STALE_TIME_MS.anomaliesInWindow,
    placeholderData: keepPreviousData,
    retry: 1,
  });

  const anomalies = useMemo<AnomalyRow[]>(() => {
    const flagged: AnomalyRow[] = [];
    for (const row of query.data?.rows ?? []) {
      const score = row.anomaly_score;
      if (typeof score !== "number") continue;
      flagged.push({ ...row, score, severity: getSeverity({ score, threshold }) });
    }
    return flagged.sort((a, b) => b.score - a.score);
  }, [query.data, threshold]);

  return {
    anomalies,
    isLoading: query.isPending && query.isFetching,
    isRefetching: query.isFetching && !query.isPending,
    isError: query.isError,
    error: query.error,
  };
}
