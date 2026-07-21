"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { parseAsBoolean, parseAsIsoDateTime, parseAsString, useQueryStates } from "nuqs";
import { useEffect, useRef } from "react";

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

/** Skip URL rewrites when the window barely moved (avoids history spam). */
const WINDOW_WRITE_SLACK_MS = 1_000;

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

function datesClose(a: Date, b: Date, slackMs = WINDOW_WRITE_SLACK_MS): boolean {
  return Math.abs(a.getTime() - b.getTime()) <= slackMs;
}

/**
 * URL-scoped readings: tenant/gh/from/to all live in the query string.
 * In live mode the window rolls forward on the refresh interval, anchored to
 * max(now, newest stored timestamp) so mock/future-dated streams stay in view.
 *
 * Background URL writes are suppressed after the user starts a cross-page
 * navigation — otherwise ``router.replace`` from a live roll / re-anchor can
 * cancel the soft navigation and leave Monitoring/Climate/Gas stuck.
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
  const scopeRef = useRef({ tenant, gh });
  scopeRef.current = { tenant, gh };
  const fromRef = useRef(from);
  const toRef = useRef(to);
  fromRef.current = from;
  toRef.current = to;
  /** Bumped on sidebar / in-app path changes so in-flight URL writes abort. */
  const urlWriteGenRef = useRef(0);
  const writeWindowRef = useRef((next: { from: Date; to: Date }, gen: number): void => {
    if (gen !== urlWriteGenRef.current) return;
    const curFrom = fromRef.current;
    const curTo = toRef.current;
    if (
      curFrom !== null &&
      curTo !== null &&
      datesClose(curFrom, next.from) &&
      datesClose(curTo, next.to)
    ) {
      return;
    }
    void setParamsRef.current({ from: next.from, to: next.to });
  });

  useEffect(() => {
    function onPointerDown(event: PointerEvent): void {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest("a[href]");
      if (!(anchor instanceof HTMLAnchorElement)) return;
      const href = anchor.getAttribute("href");
      if (!href || href.startsWith("#")) return;
      try {
        const next = new URL(href, window.location.href);
        if (next.origin !== window.location.origin) return;
        if (next.pathname !== window.location.pathname) {
          urlWriteGenRef.current += 1;
        }
      } catch {
        // Ignore malformed hrefs.
      }
    }
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, []);

  // While live with a preset period, roll the lookback window forward.
  // Anchor to newest stored data when it is ahead of wall-clock (demo SCADA).
  useEffect(() => {
    if (!live) return;

    let cancelled = false;

    async function rollWindow(): Promise<void> {
      const hours = lookbackRef.current;
      if (hours === null) return;
      const gen = urlWriteGenRef.current;
      let maxTimestamp: Date | null = null;
      try {
        const bounds = await fetchReadingsBounds(scopeRef.current);
        if (bounds.max_timestamp) maxTimestamp = new Date(bounds.max_timestamp);
      } catch {
        // Fall back to wall-clock via periodWindowEnd.
      }
      if (cancelled) return;
      const end = periodWindowEnd({ maxTimestamp, live: true });
      writeWindowRef.current(windowForPeriod(hours, end), gen);
    }

    void rollWindow();
    const id = window.setInterval(() => void rollWindow(), refreshSeconds * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [live, refreshSeconds]);

  // One-shot: if a preset URL window ends far after the newest stored row
  // (typical wall-clock "Last hour" on a historical dataset), shift it to the
  // data max so the chart/table fill. Custom ranges are never rewritten.
  useEffect(() => {
    if (live || from === null || to === null) return;
    const hours = periodHours(matchPeriodId(from, to));
    if (hours === null) return;

    let cancelled = false;
    const gen = urlWriteGenRef.current;
    void (async () => {
      try {
        const bounds = await fetchReadingsBounds({ tenant, gh });
        if (cancelled || !bounds.max_timestamp) return;
        const maxTimestamp = new Date(bounds.max_timestamp);
        if (to.getTime() - maxTimestamp.getTime() <= STALE_PRESET_SLACK_MS) return;
        const end = periodWindowEnd({ maxTimestamp, live: false });
        writeWindowRef.current(windowForPeriod(hours, end), gen);
      } catch {
        // Keep the URL window; the readings query still runs as-is.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [tenant, gh, live, from, to]);

  const query = useQuery<RecentReadingsResponse | WindowReadingsResponse, Error>({
    queryKey: ["readings", tenant, gh, from?.toISOString() ?? "recent", to?.toISOString() ?? ""],
    queryFn: (): Promise<RecentReadingsResponse | WindowReadingsResponse> =>
      hasWindow
        ? fetchWindowReadings({ tenant, gh, from, to, maxPoints: DEFAULT_MAX_POINTS })
        : fetchRecentReadings({ tenant, gh, maxPoints: DEFAULT_MAX_POINTS }),
    staleTime: live ? 0 : STALE_TIME_MS.windowedReadings,
    refetchInterval: live ? refreshSeconds * 1000 : false,
    // Never keep a previous (often much wider) window while live — that is what
    // made "Last 15 min" charts stretch across months after Go Live.
    placeholderData: live ? undefined : keepPreviousData,
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
