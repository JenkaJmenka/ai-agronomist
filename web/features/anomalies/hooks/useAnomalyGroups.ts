"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import {
  type AnomalyGroupsResult,
  fetchAnomalyGroups,
  type ServerGroupBy,
} from "@/features/anomalies/api/enriched";
import { STALE_TIME_MS } from "@/lib/query";
import { useScope } from "@/lib/useScope";

export interface AnomalyGroupsState {
  readonly groups: AnomalyGroupsResult["groups"];
  readonly isLoading: boolean;
  /** Group-by/threshold changed; stale groups stay on screen while loading. */
  readonly isRefetching: boolean;
  readonly isError: boolean;
}

/**
 * Server-side anomaly grouping (G3) — used by the Table view once the
 * flagged row count is large enough that client grouping would churn.
 */
export function useAnomalyGroups(params: {
  windowStart: string | null;
  windowEnd: string | null;
  threshold: number;
  groupBy: ServerGroupBy;
  enabled: boolean;
}): AnomalyGroupsState {
  const { tenant, gh } = useScope();
  const { windowStart, windowEnd, threshold, groupBy, enabled } = params;

  const query = useQuery<AnomalyGroupsResult, Error>({
    queryKey: ["anomaly-groups", tenant, gh, windowStart, windowEnd, threshold, groupBy],
    queryFn: () =>
      fetchAnomalyGroups({
        tenant,
        gh,
        start: windowStart ?? "",
        end: windowEnd ?? "",
        threshold,
        groupBy,
      }),
    enabled: enabled && windowStart !== null && windowEnd !== null,
    staleTime: STALE_TIME_MS.anomaliesInWindow,
    placeholderData: keepPreviousData,
    retry: 1,
  });

  return {
    groups: query.data?.groups ?? [],
    isLoading: query.isPending && query.isFetching,
    isRefetching: query.isFetching && !query.isPending,
    isError: query.isError,
  };
}
