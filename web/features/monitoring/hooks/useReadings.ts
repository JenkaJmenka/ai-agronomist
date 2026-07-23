"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { parseAsBoolean, parseAsIsoDateTime, parseAsString, useQueryStates } from "nuqs";
import { useEffect, useRef, useState } from "react";

import {
  fetchReadingsBounds,
  fetchRecentReadings,
  fetchWindowReadings,
  type RecentReadingsResponse,
  type SensorRow,
  type WindowReadingsResponse,
} from "@/features/monitoring/api/readings";
import { matchPeriodId, periodHours, periodWindowEnd, windowForPeriod } from "@/lib/periodPresets";
import { STALE_TIME_MS } from "@/lib/query";
import { useRefreshInterval } from "@/lib/useRefreshInterval";

/** Default chart budget until backend G1 downsampling is live everywhere. */
const DEFAULT_MAX_POINTS = 2000;

/** Re-anchor preset windows when URL `to` is this far past the newest stored row. */
const STALE_PRESET_SLACK_MS = 5 * 60_000;

/** Stable empty list — ``?? []`` would allocate every render and infinite-loop effects. */
const EMPTY_ROWS: SensorRow[] = [];

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
  /** Effective lookback window used for fetch + client filters. */
  readonly from: Date | null;
  readonly to: Date | null;
}

/**
 * URL-scoped readings: tenant/gh/from/to all live in the query string.
 *
 * While live, the lookback window rolls in React state only — never via
 * ``router.replace``. Background URL writes cancel Next.js soft navigations
 * (sidebar Monitoring ↔ Climate ↔ Gas stays stuck on the first page).
 */
export function useReadings(): ReadingsState {
  const [{ tenant, gh, from, to, live }] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
    from: parseAsIsoDateTime,
    to: parseAsIsoDateTime,
    live: parseAsBoolean.withDefault(false),
  });
  const [refreshSeconds] = useRefreshInterval();

  /** Live-only rolling window — must not touch the URL. */
  const [liveWindow, setLiveWindow] = useState<{ from: Date; to: Date } | null>(null);
  /** One-shot historical re-anchor override (also written to URL carefully). */
  const [anchorWindow, setAnchorWindow] = useState<{ from: Date; to: Date } | null>(null);

  const lookbackHours = from !== null && to !== null ? periodHours(matchPeriodId(from, to)) : null;
  const lookbackRef = useRef(lookbackHours);
  lookbackRef.current = lookbackHours;
  const scopeRef = useRef({ tenant, gh });
  scopeRef.current = { tenant, gh };

  // Clear live/anchor overrides when leaving live or changing scope.
  useEffect(() => {
    if (!live) setLiveWindow(null);
  }, [live]);
  useEffect(() => {
    setAnchorWindow(null);
  }, [tenant, gh]);

  // Live: roll lookback in React state only (never setParams / router.replace).
  useEffect(() => {
    if (!live) return;

    let cancelled = false;

    async function rollWindow(): Promise<void> {
      const hours = lookbackRef.current;
      if (hours === null) return;
      let maxTimestamp: Date | null = null;
      try {
        const bounds = await fetchReadingsBounds(scopeRef.current);
        if (bounds.max_timestamp) maxTimestamp = new Date(bounds.max_timestamp);
      } catch {
        // Wall-clock fallback via periodWindowEnd.
      }
      if (cancelled) return;
      const end = periodWindowEnd({ maxTimestamp, live: true });
      setLiveWindow(windowForPeriod(hours, end));
    }

    void rollWindow();
    const id = window.setInterval(() => void rollWindow(), refreshSeconds * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [live, refreshSeconds]);

  // Historical: one-shot re-anchor of stale wall-clock presets onto data max.
  // Prefer local override; URL sync is deferred and aborted if the user navigates.
  useEffect(() => {
    if (live || from === null || to === null) return;
    const hours = periodHours(matchPeriodId(from, to));
    if (hours === null) return;

    let cancelled = false;
    void (async () => {
      try {
        const bounds = await fetchReadingsBounds({ tenant, gh });
        if (cancelled || !bounds.max_timestamp) return;
        const maxTimestamp = new Date(bounds.max_timestamp);
        if (to.getTime() - maxTimestamp.getTime() <= STALE_PRESET_SLACK_MS) return;
        const end = periodWindowEnd({ maxTimestamp, live: false });
        const next = windowForPeriod(hours, end);
        setAnchorWindow(next);
        // Do not call setParams / router.replace here — that cancels soft
        // navigations when the sidebar is clicked during the bounds fetch.
      } catch {
        // Keep the URL window; the readings query still runs as-is.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [tenant, gh, live, from, to]);

  const effectiveFrom = live ? (liveWindow?.from ?? from) : (anchorWindow?.from ?? from);
  const effectiveTo = live ? (liveWindow?.to ?? to) : (anchorWindow?.to ?? to);
  const hasWindow = effectiveFrom !== null && effectiveTo !== null;

  const query = useQuery<RecentReadingsResponse | WindowReadingsResponse, Error>({
    queryKey: [
      "readings",
      tenant,
      gh,
      effectiveFrom?.toISOString() ?? "recent",
      effectiveTo?.toISOString() ?? "",
    ],
    queryFn: ({ signal }): Promise<RecentReadingsResponse | WindowReadingsResponse> =>
      hasWindow
        ? fetchWindowReadings({
            tenant,
            gh,
            from: effectiveFrom,
            to: effectiveTo,
            maxPoints: DEFAULT_MAX_POINTS,
            signal,
          })
        : fetchRecentReadings({ tenant, gh, maxPoints: DEFAULT_MAX_POINTS, signal }),
    staleTime: live ? 0 : STALE_TIME_MS.windowedReadings,
    // Live ticks come from SSE (`useLiveReadings`); rolling `liveWindow` already
    // changes the queryKey when the lookback re-anchors. A parallel refetchInterval
    // doubled main-thread + network work and froze clicks during live mode.
    refetchInterval: false,
    // Never keep a previous (often much wider) window while live — that is what
    // made "Last 15 min" charts stretch across months after Go Live.
    placeholderData: live ? undefined : keepPreviousData,
  });

  const rows = query.data?.rows ?? EMPTY_ROWS;
  return {
    rows,
    isLoading: query.isPending,
    // Placeholder = keepPreviousData for a new window — treat as refetch so
    // RefetchingBlock dims while the client still shows the prior range.
    isRefetching: query.isFetching && (!query.isPending || query.isPlaceholderData),
    isError: query.isError,
    error: query.error,
    downsampled: Boolean((query.data as { downsampled?: boolean } | undefined)?.downsampled),
    originalCount:
      (query.data as { original_count?: number } | undefined)?.original_count ?? rows.length,
    windowLabel: hasWindow
      ? `${effectiveFrom.toISOString().slice(0, 16)} → ${effectiveTo.toISOString().slice(0, 16)}`
      : "Most recent data (7 days)",
    from: effectiveFrom,
    to: effectiveTo,
  };
}
