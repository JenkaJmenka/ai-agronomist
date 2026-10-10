"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import {
  type AnomalyEpisodesResult,
  fetchAnomalyEpisodes,
} from "@/features/anomalies/api/episodes";
import { STALE_TIME_MS } from "@/lib/query";
import { useScope } from "@/lib/useScope";

export interface AnomalyEpisodesState {
  readonly episodes: AnomalyEpisodesResult["episodes"];
  /** Flagged rows in the window — the samples these episodes merge. */
  readonly anomalyCount: number | null;
  readonly isLoading: boolean;
  /** Window/threshold changed; stale episodes stay on screen while loading. */
  readonly isRefetching: boolean;
  readonly isError: boolean;
  readonly error: Error | null;
}

/**
 * Merged anomaly episodes for the current window.
 *
 * The server groups contiguous flagged samples by (channel, detector,
 * direction), so one continuous event arrives as one row instead of N
 * five-minute samples.
 */
export function useAnomalyEpisodes(params: {
  windowStart: string | null;
  windowEnd: string | null;
  threshold: number;
  enabled: boolean;
}): AnomalyEpisodesState {
  const { tenant, gh } = useScope();
  const { windowStart, windowEnd, threshold, enabled } = params;

  const query = useQuery<AnomalyEpisodesResult, Error>({
    queryKey: ["anomaly-episodes", tenant, gh, windowStart, windowEnd, threshold],
    queryFn: ({ signal }) =>
      fetchAnomalyEpisodes({
        tenant,
        gh,
        start: windowStart ?? "",
        end: windowEnd ?? "",
        threshold,
        signal,
      }),
    enabled: enabled && windowStart !== null && windowEnd !== null,
    staleTime: STALE_TIME_MS.anomaliesInWindow,
    placeholderData: keepPreviousData,
    retry: 1,
  });

  return {
    episodes: query.data?.episodes ?? [],
    anomalyCount: query.data?.anomalyCount ?? null,
    isLoading: query.isPending && query.isFetching,
    isRefetching: query.isFetching && !query.isPending,
    isError: query.isError,
    error: query.error,
  };
}
