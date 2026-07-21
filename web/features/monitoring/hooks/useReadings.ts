"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { parseAsBoolean, parseAsIsoDateTime, parseAsString, useQueryStates } from "nuqs";
import { useEffect, useRef } from "react";

import {
  fetchRecentReadings,
  fetchWindowReadings,
  type RecentReadingsResponse,
  type SensorRow,
  type WindowReadingsResponse,
} from "@/features/monitoring/api/readings";
import { matchPeriodId, periodHours, windowForPeriod } from "@/lib/periodPresets";
import { STALE_TIME_MS } from "@/lib/query";
import { useRefreshInterval } from "@/lib/useRefreshInterval";

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
 * URL-scoped readings: tenant/gh/from/to all live in the query string.
 * In live mode the window rolls forward on the refresh interval.
 */
export function useReadings(): ReadingsState {
  const [{ tenant, gh, from, to, live }, setParams] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
    from: parseAsIsoDateTime,
    to: parseAsIsoDateTime,
    live: parseAsBoolean.withDefault(false),
  });
  const [refreshSeconds] = useRefreshInterval();

  const hasWindow = from !== null && to !== null;
  const lookbackHours = hasWindow ? periodHours(matchPeriodId(from, to)) : null;
  const lookbackRef = useRef(lookbackHours);
  lookbackRef.current = lookbackHours;
  const setParamsRef = useRef(setParams);
  setParamsRef.current = setParams;

  // While live with a preset period, roll the lookback window forward so charts
  // stay anchored to "now − period". Custom absolute windows are left alone.
  useEffect(() => {
    if (!live) return;

    function rollWindow(): void {
      const hours = lookbackRef.current;
      if (hours === null) return;
      const next = windowForPeriod(hours);
      void setParamsRef.current({ from: next.from, to: next.to });
    }

    const id = window.setInterval(rollWindow, refreshSeconds * 1000);
    return () => window.clearInterval(id);
  }, [live, refreshSeconds]);

  const query = useQuery<RecentReadingsResponse | WindowReadingsResponse, Error>({
    queryKey: ["readings", tenant, gh, from?.toISOString() ?? "recent", to?.toISOString() ?? ""],
    queryFn: (): Promise<RecentReadingsResponse | WindowReadingsResponse> =>
      hasWindow
        ? fetchWindowReadings({ tenant, gh, from, to, maxPoints: DEFAULT_MAX_POINTS })
        : fetchRecentReadings({ tenant, gh, maxPoints: DEFAULT_MAX_POINTS }),
    staleTime: live ? 0 : STALE_TIME_MS.windowedReadings,
    refetchInterval: live ? refreshSeconds * 1000 : false,
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
