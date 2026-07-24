"use client";

import { parseAsBoolean, parseAsFloat, parseAsString, useQueryState } from "nuqs";
import { Suspense, useDeferredValue, useMemo } from "react";

import { Card } from "@/components/ui/Card";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { useEnrichedAnomalies } from "@/features/anomalies/hooks/useEnrichedAnomalies";
import {
  applyDisplayChannelAliases,
  discoverChannels,
  mergeChannelLists,
  type SensorRow,
} from "@/features/monitoring/api/readings";
import { ChannelToggles } from "@/features/monitoring/components/ChannelToggles";
import { LiveAnomalyStrip } from "@/features/monitoring/components/LiveAnomalyStrip";
import { LiveBadge } from "@/features/monitoring/components/LiveBadge";
import { ReadingsTable } from "@/features/monitoring/components/ReadingsTable";
import { SensorTiles } from "@/features/monitoring/components/SensorTiles";
import { SensorTimeSeriesChart } from "@/features/monitoring/components/SensorTimeSeriesChart";
import { useChannelVisibility } from "@/features/monitoring/hooks/useChannelVisibility";
import { useLiveReadings } from "@/features/monitoring/hooks/useLiveReadings";
import { useReadings } from "@/features/monitoring/hooks/useReadings";
import { useSetpoints } from "@/features/monitoring/hooks/useSetpoints";
import { shouldPaintAnomalyHighlight } from "@/features/monitoring/lib/anomalyHighlight";
import { SharedViewControls } from "@/features/shell/SharedViewControls";
import { rowsForActiveWindow } from "@/lib/periodPresets";
import { DEFAULT_SCORE_THRESHOLD } from "@/lib/severity";
import { useChartRangeSelect } from "@/lib/useChartRangeSelect";
import { useChartViewWindow } from "@/lib/useChartViewWindow";
import { useScope } from "@/lib/useScope";

interface MonitoringViewProps {
  readonly title: string;
  /** Stable page identifier used to scope the chart baseline (e.g. `"monitoring"`). */
  readonly pageId: string;
  /** Restrict to these channels (e.g. climate → temp/humid prefixes). Empty = all. */
  readonly channelFilter?: readonly string[];
  /**
   * How ``channelFilter`` matches discovered names.
   * - ``includes`` (default): substring match (climate ``temp`` → ``temperature``)
   * - ``exact``: only exact channel names (gas page must not pick climate ``co2``)
   */
  readonly channelMatch?: "includes" | "exact";
}

/**
 * Merge historical + live rows; live wins on duplicate timestamp.
 * Result is sorted chronologically (oldest → newest) for uPlot.
 */
function dedupeByTimestamp(historical: SensorRow[], live: SensorRow[]): SensorRow[] {
  const map = new Map<string, SensorRow>();
  for (const row of historical) map.set(row.timestamp, row);
  for (const row of live) map.set(row.timestamp, row);
  return Array.from(map.values()).sort((a, b) =>
    a.timestamp < b.timestamp ? -1 : a.timestamp > b.timestamp ? 1 : 0,
  );
}

function MonitoringBody({
  title,
  pageId,
  channelFilter,
  channelMatch = "includes",
}: MonitoringViewProps) {
  const {
    rows: historicalRows,
    isLoading,
    isRefetching,
    isError,
    error,
    windowLabel,
    downsampled,
    from,
    to,
  } = useReadings();

  const { tenant, gh } = useScope();
  const [live] = useQueryState("live", parseAsBoolean.withDefault(false));
  const [channelParam] = useQueryState("channel", parseAsString);
  const [threshold] = useQueryState("threshold", parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD));

  const { rows: liveRowsRaw, anomalies, stale, transport, sourceStatuses } = useLiveReadings(live);
  const { setpoints, updateSetpoint, resetToDefaults } = useSetpoints();

  const historicalAliased = useMemo(
    () => applyDisplayChannelAliases(historicalRows),
    [historicalRows],
  );
  const liveRows = useMemo(() => applyDisplayChannelAliases(liveRowsRaw), [liveRowsRaw]);

  // Unfiltered merge drives channel discovery; period filter only clips once settled.
  const mergedRows = useMemo(
    () => (live ? dedupeByTimestamp(historicalAliased, liveRows) : historicalAliased),
    [live, historicalAliased, liveRows],
  );
  // While live, open the upper clip to the newest SSE tick (or wall-clock now)
  // so points arriving after the last liveWindow roll are not filtered out.
  const newestLiveMs = useMemo(() => {
    if (!live || liveRows.length === 0) return null;
    const ms = Date.parse(liveRows[liveRows.length - 1]?.timestamp ?? "");
    return Number.isFinite(ms) ? ms : null;
  }, [live, liveRows]);
  const clipTo = useMemo(() => {
    if (!to) return null;
    if (!live) return to;
    const candidates = [to.getTime(), Date.now()];
    if (newestLiveMs !== null) candidates.push(newestLiveMs);
    return new Date(Math.max(...candidates));
  }, [to, live, newestLiveMs]);
  const rows = useMemo(
    () =>
      rowsForActiveWindow({
        rows: mergedRows,
        from,
        to: clipTo,
        isRefetching,
      }),
    [mergedRows, from, clipTo, isRefetching],
  );
  // Chart/table can lag a frame behind tiles so nav/clicks stay responsive under live SSE.
  const deferredRows = useDeferredValue(rows);

  // Envelope for G4 enrichment — prefer URL/live lookback bounds so the queryKey
  // does not churn on every SSE flush (rows[0]/at(-1) move every tick).
  const windowStart = from?.toISOString() ?? deferredRows[0]?.timestamp ?? null;
  const windowEnd = to?.toISOString() ?? deferredRows.at(-1)?.timestamp ?? null;
  // Defer G4 until readings settle; cap at 50 so Monitoring never waits on limit=500.
  const enrichment = useEnrichedAnomalies({
    windowStart,
    windowEnd,
    threshold,
    enabled: deferredRows.length > 0 && !isRefetching,
    limit: 50,
  });

  // Dim + debounced URL write for chart drag-select / scroll-zoom.
  const { isDimmed, onChartRangeSelect } = useChartRangeSelect({ isReadingsBusy: isRefetching });
  const { chartViewWindow, onTableRowSelect } = useChartViewWindow({
    from,
    to,
    scopeKey: `${tenant}/${gh}`,
  });

  /** Detector-attributed sensors per timestamp (enrichment + live SSE). */
  const affectedByTimestamp = useMemo(() => {
    const map = new Map<string, readonly string[]>();
    const put = (ts: string, sensors: readonly string[]) => {
      if (sensors.length === 0) return;
      map.set(ts, sensors);
      const ms = Date.parse(ts);
      if (Number.isFinite(ms)) map.set(new Date(ms).toISOString(), sensors);
    };
    for (const [ts, detail] of enrichment.byTimestamp) {
      put(ts, detail.affectedSensors);
    }
    for (const anomaly of anomalies) {
      put(anomaly.timestamp, anomaly.affected_channels);
    }
    return map;
  }, [enrichment.byTimestamp, anomalies]);

  // Live buffer alone can carry channels that the windowed history lost.
  const discoveredNow = useMemo(() => {
    const fromMerge = discoverChannels(mergedRows);
    return live ? mergeChannelLists(fromMerge, discoverChannels(liveRows)) : fromMerge;
  }, [mergedRows, live, liveRows]);

  // Channel visibility state machine (session memory + anomaly jump + manual toggles).
  const { channels, visible, jumpFocus, toggleChannel } = useChannelVisibility({
    discoveredNow,
    channelParam,
    tenant,
    gh,
    live,
    channelFilter,
    channelMatch,
  });

  const paintAnomalyHighlight = shouldPaintAnomalyHighlight({
    channelParam,
    pageChannels: channels,
    visible,
  });
  const anomalyFocusVisible = useMemo(
    () => (jumpFocus.length > 0 ? jumpFocus.filter((ch) => visible.has(ch)) : undefined),
    [jumpFocus, visible],
  );

  // Scope key ties the chart baseline to this page + tenant + greenhouse.
  const scopeKey = `${pageId}:${tenant}:${gh}`;

  if (isError) {
    return (
      <div className="mx-auto max-w-6xl space-y-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">{title}</h1>
            <p className="text-sm text-text-muted tabular-nums">{windowLabel}</p>
          </div>
          <SharedViewControls
            pageId={pageId}
            channels={channels}
            setpoints={setpoints}
            onUpdateSetpoint={updateSetpoint}
            onResetSetpoints={resetToDefaults}
            trailing={
              live ? (
                <LiveBadge transport={transport} stale={stale} sourceStatuses={sourceStatuses} />
              ) : null
            }
          />
        </div>
        <Card>
          <p role="alert" className="py-8 text-center text-sm text-sev-critical">
            {error?.message ?? "Failed to load readings"}
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      {/* ── Header ── */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{title}</h1>
          <p className="text-sm text-text-muted tabular-nums">
            {windowLabel}
            {downsampled ? " · downsampled" : ""}
          </p>
        </div>
        <SharedViewControls
          pageId={pageId}
          channels={channels}
          setpoints={setpoints}
          onUpdateSetpoint={updateSetpoint}
          onResetSetpoints={resetToDefaults}
          trailing={
            live ? (
              <LiveBadge transport={transport} stale={stale} sourceStatuses={sourceStatuses} />
            ) : null
          }
        />
      </div>

      {isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-80 w-full" />
          <Skeleton className="h-40 w-full" />
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
          {/* ── Live sensor tiles ── */}
          <SensorTiles
            rows={rows}
            channels={channels}
            setpoints={setpoints}
            isLive={live}
            isStale={stale}
            isLoading={false}
          />

          <Card title="Sensor channels">
            <ChannelToggles channels={channels} visible={visible} onToggle={toggleChannel} />
          </Card>

          {live && anomalies.length > 0 && (
            <LiveAnomalyStrip anomalies={anomalies} threshold={threshold} />
          )}

          <Card title="Time series">
            <SensorTimeSeriesChart
              rows={deferredRows}
              channels={channels}
              visible={visible}
              threshold={threshold}
              setpoints={setpoints}
              onRangeSelect={onChartRangeSelect}
              viewWindow={chartViewWindow}
              showAnomalyBands={paintAnomalyHighlight}
              anomalyFocusVisible={anomalyFocusVisible}
              affectedByTimestamp={affectedByTimestamp}
              scopeKey={scopeKey}
            />
          </Card>
          <Card title={`Readings (${deferredRows.length.toLocaleString()} rows, newest first)`}>
            <ReadingsTable
              rows={deferredRows}
              channels={channels}
              visible={visible}
              threshold={threshold}
              setpoints={setpoints}
              affectedByTimestamp={affectedByTimestamp}
              highlightAnomalies={paintAnomalyHighlight}
              onWindowSelect={onTableRowSelect}
            />
          </Card>
        </RefetchingBlock>
      )}
    </div>
  );
}

export function MonitoringView(props: MonitoringViewProps) {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-80 w-full" />
        </div>
      }
    >
      <MonitoringBody {...props} />
    </Suspense>
  );
}
