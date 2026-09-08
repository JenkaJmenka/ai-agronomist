"use client";

import { parseAsBoolean, parseAsFloat, useQueryState } from "nuqs";
import { Suspense, useDeferredValue, useEffect, useMemo, useRef } from "react";

import { Card } from "@/components/ui/Card";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { useAnomalyCount } from "@/features/anomalies/hooks/useAnomalyCount";
import { useFlaggedWindow } from "@/features/anomalies/hooks/useFlaggedWindow";
import { useHomeLayoutPrefs } from "@/features/app-configuration/hooks/useHomeLayoutPrefs";
import { HomeAnomalyList } from "@/features/home/components/HomeAnomalyList";
import { HomeControls } from "@/features/home/components/HomeControls";
import { HomeStatusBar } from "@/features/home/components/HomeStatusBar";
import { homeChartVisible, selectHomeChannels } from "@/features/home/lib/homeChannels";
import { buildRecentAnomalies, countSeverities } from "@/features/home/lib/recentAnomalies";
import {
  applyDisplayChannelAliases,
  discoverChannels,
  mergeChannelLists,
  type SensorRow,
} from "@/features/monitoring/api/readings";
import { LiveAnomalyStrip } from "@/features/monitoring/components/LiveAnomalyStrip";
import { SensorTiles } from "@/features/monitoring/components/SensorTiles";
import { SensorTimeSeriesChart } from "@/features/monitoring/components/SensorTimeSeriesChart";
import { useLiveReadings } from "@/features/monitoring/hooks/useLiveReadings";
import { useReadings } from "@/features/monitoring/hooks/useReadings";
import { useSetpoints } from "@/features/monitoring/hooks/useSetpoints";
import { periodHours, rowsForActiveWindow, windowForPeriod } from "@/lib/periodPresets";
import { DEFAULT_SCORE_THRESHOLD } from "@/lib/severity";
import { useChartRangeSelect } from "@/lib/useChartRangeSelect";
import { useDateRange } from "@/lib/useDateRange";
import { useScope } from "@/lib/useScope";

const PAGE_ID = "home";

function dedupeByTimestamp(historical: SensorRow[], live: SensorRow[]): SensorRow[] {
  const map = new Map<string, SensorRow>();
  for (const row of historical) map.set(row.timestamp, row);
  for (const row of live) map.set(row.timestamp, row);
  return Array.from(map.values()).sort((a, b) =>
    a.timestamp < b.timestamp ? -1 : a.timestamp > b.timestamp ? 1 : 0,
  );
}

function carryForwardChannels(rows: readonly SensorRow[]): SensorRow[] {
  const carry: Record<string, number> = {};
  return rows.map((row) => {
    const next: Record<string, unknown> = { ...row };
    let changed = false;
    for (const [key, value] of Object.entries(carry)) {
      if (next[key] == null) {
        next[key] = value;
        changed = true;
      }
    }
    for (const [key, value] of Object.entries(row)) {
      if (key === "timestamp" || key === "anomaly_score") continue;
      if (typeof value === "number") carry[key] = value;
    }
    return changed ? (next as SensorRow) : row;
  });
}

function HomeBody() {
  const { tenant, gh } = useScope();
  const { prefs, hydrated } = useHomeLayoutPrefs();
  const [{ from: urlFrom, to: urlTo }, setRange] = useDateRange();
  const appliedPeriod = useRef(false);

  useEffect(() => {
    if (!hydrated || appliedPeriod.current) return;
    if (urlFrom !== null || urlTo !== null) {
      appliedPeriod.current = true;
      return;
    }
    const hours = periodHours(prefs.defaultPeriodId);
    if (hours === null) {
      appliedPeriod.current = true;
      return;
    }
    appliedPeriod.current = true;
    const { from, to } = windowForPeriod(hours);
    void setRange({ from, to });
  }, [hydrated, prefs.defaultPeriodId, urlFrom, urlTo, setRange]);

  const {
    rows: historicalRows,
    isLoading,
    isRefetching,
    isError,
    error,
    windowLabel,
    from,
    to,
  } = useReadings();

  const [live] = useQueryState("live", parseAsBoolean.withDefault(false));
  const [threshold] = useQueryState("threshold", parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD));
  const { rows: liveRowsRaw, anomalies, transport, stale, sourceStatuses } = useLiveReadings(live);
  const { setpoints } = useSetpoints();

  const historicalAliased = useMemo(
    () => applyDisplayChannelAliases(historicalRows),
    [historicalRows],
  );
  const liveRows = useMemo(() => applyDisplayChannelAliases(liveRowsRaw), [liveRowsRaw]);
  const mergedRows = useMemo(
    () => (live ? dedupeByTimestamp(historicalAliased, liveRows) : historicalAliased),
    [live, historicalAliased, liveRows],
  );
  const rows = useMemo(
    () => rowsForActiveWindow({ rows: mergedRows, from, to, isRefetching }),
    [mergedRows, from, to, isRefetching],
  );
  const deferredChartRows = useDeferredValue(rows);
  const tileRows = useMemo(() => carryForwardChannels(rows), [rows]);

  const windowStart = from?.toISOString() ?? deferredChartRows[0]?.timestamp ?? null;
  const windowEnd = to?.toISOString() ?? deferredChartRows.at(-1)?.timestamp ?? null;

  const anomalyCount = useAnomalyCount({
    windowStart,
    windowEnd,
    threshold,
    enabled: windowStart !== null && windowEnd !== null,
  });
  const flagged = useFlaggedWindow({
    windowStart,
    windowEnd,
    threshold,
    enabled: prefs.showAnomalyList && windowStart !== null && windowEnd !== null && !live,
  });

  const recentItems = useMemo(
    () =>
      buildRecentAnomalies({
        live,
        liveAnomalies: anomalies,
        flagged: flagged.anomalies,
        threshold,
        limit: 8,
      }),
    [live, anomalies, flagged.anomalies, threshold],
  );
  const severityCounts = useMemo(() => countSeverities(recentItems), [recentItems]);

  const discovered = useMemo(() => {
    const fromMerge = discoverChannels(mergedRows);
    return live ? mergeChannelLists(fromMerge, discoverChannels(liveRows)) : fromMerge;
  }, [mergedRows, live, liveRows]);

  const channels = useMemo(
    () => selectHomeChannels(discovered, prefs.primaryChannels),
    [discovered, prefs.primaryChannels],
  );
  const chartVisible = useMemo(
    () => homeChartVisible(channels, prefs.primaryChannels),
    [channels, prefs.primaryChannels],
  );

  const { isDimmed, onChartRangeSelect } = useChartRangeSelect({
    isReadingsBusy: !live && isRefetching,
  });
  const scopeKey = `${PAGE_ID}:${tenant}:${gh}`;

  if (isError) {
    return (
      <div className="mx-auto max-w-6xl space-y-6" data-testid="home-page">
        <HomeHeader windowLabel={windowLabel} />
        <Card>
          <p role="alert" className="py-8 text-center text-sm text-sev-critical">
            {error?.message ?? "Failed to load readings"}
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6" data-testid="home-page">
      <HomeHeader windowLabel={windowLabel} />

      {isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
      ) : rows.length === 0 ? (
        <Card>
          <p className="py-8 text-center text-sm text-text-muted">
            {live
              ? "Waiting for live data — streaming started."
              : "No sensor readings in the selected window. Widen the range or pick another tenant/greenhouse."}
          </p>
        </Card>
      ) : (
        <RefetchingBlock isRefetching={isDimmed} className="space-y-6">
          {prefs.showStatusBar ? (
            <HomeStatusBar
              transport={transport}
              stale={stale}
              sourceStatuses={sourceStatuses}
              anomalyCount={anomalyCount.count}
              critical={severityCounts.critical}
              warning={severityCounts.warning}
              lstm={severityCounts.lstm}
              isCountLoading={anomalyCount.isLoading}
            />
          ) : null}

          {prefs.showTiles ? (
            <SensorTiles
              rows={tileRows}
              channels={channels}
              setpoints={setpoints}
              isLive={live}
              isStale={stale}
              isLoading={false}
            />
          ) : null}

          {live && anomalies.length > 0 ? (
            <LiveAnomalyStrip anomalies={anomalies} threshold={threshold} />
          ) : null}

          {prefs.showChart ? (
            <Card title="Climate overview">
              <SensorTimeSeriesChart
                rows={deferredChartRows}
                channels={channels}
                visible={chartVisible}
                threshold={threshold}
                onRangeSelect={onChartRangeSelect}
                showAnomalyBands
                scopeKey={scopeKey}
              />
            </Card>
          ) : null}

          {prefs.showAnomalyList ? (
            <Card title="Recent anomalies">
              <HomeAnomalyList
                items={recentItems}
                threshold={threshold}
                isLoading={!live && flagged.isLoading}
              />
            </Card>
          ) : null}
        </RefetchingBlock>
      )}
    </div>
  );
}

function HomeHeader({ windowLabel }: { readonly windowLabel: string }) {
  const { tenant, gh } = useScope();
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-bold">AI Agronomist</h1>
        <p className="text-sm text-text-muted tabular-nums">
          {tenant} · {gh} · {windowLabel}
        </p>
      </div>
      <HomeControls pageId={PAGE_ID} />
    </div>
  );
}

export function HomeView() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
      }
    >
      <HomeBody />
    </Suspense>
  );
}
