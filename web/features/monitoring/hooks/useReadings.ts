"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { parseAsIsoDateTime, parseAsString, useQueryStates } from "nuqs";

import {
  fetchRecentReadings,
  fetchWindowReadings,
  type RecentReadingsResponse,
  type SensorRow,
  type WindowReadingsResponse,
} from "@/features/monitoring/api/readings";
import { STALE_TIME_MS } from "@/lib/query";

/** Default chart budget until backend G1 downsampling is live everywhere. */
const DEFAULT_MAX_POINTS = 2000;

export interface ReadingsState {
  readonly rows: SensorRow[];
  readonly isLoading: boolean;
  /** A new window/scope is loading while stale rows stay on screen. */
  readonly isRefetching: boolean;
  readonly isError: boolean;
  readonly error: Error | null;
  readonly downsampled: boolean;
  /** Pre-downsampling row count of the window (equals rows.length otherwise). */
  readonly originalCount: number;
  readonly windowLabel: string;
}

/**
 * URL-scoped readings: tenant/gh/from/to all live in the query string, so
 * reloads and shared links reproduce the exact view (plan §7.4).
 * Without an explicit window it falls back to the most recent 7 days of data.
 */
export function useReadings(): ReadingsState {
  const [{ tenant, gh, from, to }] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
    from: parseAsIsoDateTime,
    to: parseAsIsoDateTime,
  });

  const hasWindow = from !== null && to !== null;

  const query = useQuery<RecentReadingsResponse | WindowReadingsResponse, Error>({
    queryKey: ["readings", tenant, gh, from?.toISOString() ?? "recent", to?.toISOString() ?? ""],
    queryFn: (): Promise<RecentReadingsResponse | WindowReadingsResponse> =>
      hasWindow
        ? fetchWindowReadings({ tenant, gh, from, to, maxPoints: DEFAULT_MAX_POINTS })
        : fetchRecentReadings({ tenant, gh, maxPoints: DEFAULT_MAX_POINTS }),
    staleTime: STALE_TIME_MS.windowedReadings,
    placeholderData: keepPreviousData,
  });

  const rows = query.data?.rows ?? [];
  return {
    rows,
    isLoading: query.isPending,
    isRefetching: query.isFetching && !query.isPending,
    isError: query.isError,
    error: query.error,
    downsampled: Boolean((query.data as { downsampled?: boolean } | undefined)?.downsampled),
    originalCount:
      (query.data as { original_count?: number } | undefined)?.original_count ?? rows.length,
    windowLabel: hasWindow
      ? `${from.toISOString().slice(0, 16)} → ${to.toISOString().slice(0, 16)}`
      : "Most recent data (7 days)",
  };
}
