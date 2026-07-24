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
import { rememberBoundsMax } from "@/features/monitoring/lib/boundsCache";
import {
  DEFAULT_PERIOD_ID,
  matchPeriodId,
  periodHours,
  periodWindowEnd,
  windowForPeriod,
} from "@/lib/periodPresets";
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

  /** Live-only rolling clip window — drives UI label + client filter. */
  const [liveWindow, setLiveWindow] = useState<{ from: Date; to: Date } | null>(null);
  /**
   * Stable fetch envelope while live. Rolls on a slower cadence so the 10s
   * Refresh control does not rebuild the React Query key (skeleton / dim flicker).
   */
  const [liveFetchWindow, setLiveFetchWindow] = useState<{ from: Date; to: Date } | null>(null);
  /** One-shot historical re-anchor override (also written to URL carefully). */
  const [anchorWindow, setAnchorWindow] = useState<{ from: Date; to: Date } | null>(null);

  const lookbackHours =
    from !== null && to !== null
      ? (periodHours(matchPeriodId(from, to)) ?? periodHours(DEFAULT_PERIOD_ID))
      : periodHours(DEFAULT_PERIOD_ID);
  const lookbackRef = useRef(lookbackHours);
  lookbackRef.current = lookbackHours;
  const urlFromRef = useRef(from);
  urlFromRef.current = from;
  const urlToRef = useRef(to);
  urlToRef.current = to;
  const scopeRef = useRef({ tenant, gh });
  scopeRef.current = { tenant, gh };

  // Clear live/anchor overrides when leaving live or changing scope.
  useEffect(() => {
    if (!live) {
      setLiveWindow(null);
      setLiveFetchWindow(null);
    }
  }, [live]);
  useEffect(() => {
    setAnchorWindow(null);
  }, [tenant, gh]);

  // Live: roll the clip window often (smooth chart follow) but only re-fetch
  // history when the lookback changes or on a slow cadence (≥60s).
  useEffect(() => {
    if (!live) return;

    let cancelled = false;

    function snapClip(end: Date): void {
      const hours = lookbackRef.current ?? periodHours(DEFAULT_PERIOD_ID) ?? 24;
      const spanFrom = urlFromRef.current;
      const spanTo = urlToRef.current;
      if (hours !== null) {
        setLiveWindow(windowForPeriod(hours, end));
        return;
      }
      if (spanFrom !== null && spanTo !== null) {
        const spanMs = Math.max(spanTo.getTime() - spanFrom.getTime(), 60_000);
        setLiveWindow({ from: new Date(end.getTime() - spanMs), to: end });
      }
    }

    async function snapFetch(): Promise<void> {
      const hours = lookbackRef.current ?? periodHours(DEFAULT_PERIOD_ID) ?? 24;
      const spanFrom = urlFromRef.current;
      const spanTo = urlToRef.current;

      let maxTimestamp: Date | null = null;
      try {
        const bounds = await fetchReadingsBounds(scopeRef.current);
        rememberBoundsMax(scopeRef.current.tenant, scopeRef.current.gh, bounds.max_timestamp);
        if (bounds.max_timestamp) maxTimestamp = new Date(bounds.max_timestamp);
      } catch {
        // Wall-clock via periodWindowEnd.
      }
      if (cancelled) return;

      const end = periodWindowEnd({ maxTimestamp, live: true });
      snapClip(end);

      if (hours !== null) {
        // Pad the fetch envelope so clipping can advance between refetches.
        const paddedHours = hours + Math.max(hours * 0.25, 0.25);
        setLiveFetchWindow(windowForPeriod(paddedHours, end));
        return;
      }
      if (spanFrom !== null && spanTo !== null) {
        const spanMs = Math.max(spanTo.getTime() - spanFrom.getTime(), 60_000);
        const padMs = Math.max(spanMs * 0.25, 60_000);
        setLiveFetchWindow({
          from: new Date(end.getTime() - spanMs - padMs),
          to: end,
        });
      }
    }

    // Immediate clip from wall-clock — never urlTo (that may still be a
    // historical Dec max during a period switch and flashes a wrong window).
    snapClip(new Date());
    void snapFetch();

    const clipId = window.setInterval(() => {
      snapClip(new Date());
    }, 1_000);
    const fetchEveryMs = Math.max(refreshSeconds, 60) * 1000;
    const fetchId = window.setInterval(() => void snapFetch(), fetchEveryMs);

    return () => {
      cancelled = true;
      window.clearInterval(clipId);
      window.clearInterval(fetchId);
    };
  }, [live, refreshSeconds, lookbackHours, from, to]);

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
  const fetchFrom = live ? (liveFetchWindow?.from ?? effectiveFrom) : effectiveFrom;
  const fetchTo = live ? (liveFetchWindow?.to ?? effectiveTo) : effectiveTo;
  const hasWindow = effectiveFrom !== null && effectiveTo !== null;
  const hasFetchWindow = fetchFrom !== null && fetchTo !== null;

  const query = useQuery<RecentReadingsResponse | WindowReadingsResponse, Error>({
    queryKey: [
      "readings",
      tenant,
      gh,
      fetchFrom?.toISOString() ?? "recent",
      fetchTo?.toISOString() ?? "",
    ],
    queryFn: ({ signal }): Promise<RecentReadingsResponse | WindowReadingsResponse> =>
      hasFetchWindow
        ? fetchWindowReadings({
            tenant,
            gh,
            from: fetchFrom,
            to: fetchTo,
            maxPoints: DEFAULT_MAX_POINTS,
            signal,
          })
        : fetchRecentReadings({ tenant, gh, maxPoints: DEFAULT_MAX_POINTS, signal }),
    staleTime: live ? 0 : STALE_TIME_MS.windowedReadings,
    // Live ticks come from SSE; clip window rolls in React state. Keep prior
    // rows so rolling lookback never flashes skeletons / empty charts.
    refetchInterval: false,
    placeholderData: keepPreviousData,
  });

  const rows = query.data?.rows ?? EMPTY_ROWS;
  return {
    rows,
    isLoading: query.isPending && !query.isPlaceholderData,
    // Live clip rolls must not dim the page — only user period/scope changes.
    isRefetching: !live && query.isFetching && (!query.isPending || query.isPlaceholderData),
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
