"use client";

import { parseAsBoolean, parseAsFloat, parseAsString, useQueryState } from "nuqs";
import {
  Suspense,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { Card } from "@/components/ui/Card";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { useEnrichedAnomalies } from "@/features/anomalies/hooks/useEnrichedAnomalies";
import {
  discoverChannels,
  mergeChannelLists,
  type SensorRow,
} from "@/features/monitoring/api/readings";
import { ChannelToggles } from "@/features/monitoring/components/ChannelToggles";
import { LiveAnomalyStrip } from "@/features/monitoring/components/LiveAnomalyStrip";
import { ReadingsTable } from "@/features/monitoring/components/ReadingsTable";
import { SensorTiles } from "@/features/monitoring/components/SensorTiles";
import { SensorTimeSeriesChart } from "@/features/monitoring/components/SensorTimeSeriesChart";
import { useLiveReadings } from "@/features/monitoring/hooks/useLiveReadings";
import { useReadings } from "@/features/monitoring/hooks/useReadings";
import { useSetpoints } from "@/features/monitoring/hooks/useSetpoints";
import {
  matchChannelParam,
  shouldPaintAnomalyHighlight,
} from "@/features/monitoring/lib/anomalyHighlight";
import { SharedViewControls } from "@/features/shell/SharedViewControls";
import { debounce } from "@/lib/debounce";
import { rowsForActiveWindow } from "@/lib/periodPresets";
import { measureInteraction } from "@/lib/profiling";
import { DEFAULT_SCORE_THRESHOLD } from "@/lib/severity";
import { useChartViewWindow } from "@/lib/useChartViewWindow";
import { useDateRange } from "@/lib/useDateRange";
import { useScope } from "@/lib/useScope";
import { useWindowBusyCue } from "@/lib/useWindowBusyCue";

interface MonitoringViewProps {
  readonly title: string;
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

function MonitoringBody({ title, channelFilter, channelMatch = "includes" }: MonitoringViewProps) {
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
  const [, setRange] = useDateRange();

  const { rows: liveRows, anomalies, stale } = useLiveReadings(live);
  const { setpoints, updateSetpoint, resetToDefaults } = useSetpoints();

  // Unfiltered merge drives channel discovery; period filter only clips once settled.
  const mergedRows = useMemo(
    () => (live ? dedupeByTimestamp(historicalRows, liveRows) : historicalRows),
    [live, historicalRows, liveRows],
  );
  const rows = useMemo(
    () =>
      rowsForActiveWindow({
        rows: mergedRows,
        from,
        to,
        isRefetching,
      }),
    [mergedRows, from, to, isRefetching],
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
  const { isDimmed, beginWindowChange } = useWindowBusyCue({
    isReadingsBusy: isRefetching,
  });
  const debouncedSetRange = useMemo(
    () =>
      debounce((nextFrom: Date, nextTo: Date) => {
        void setRange({ from: nextFrom, to: nextTo });
      }, 200),
    [setRange],
  );
  useEffect(() => () => debouncedSetRange.cancel(), [debouncedSetRange]);
  const onChartRangeSelect = useCallback(
    (nextFrom: Date, nextTo: Date) => {
      beginWindowChange();
      debouncedSetRange(nextFrom, nextTo);
    },
    [beginWindowChange, debouncedSetRange],
  );
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

  const discoveredNow = useMemo(() => {
    const fromMerge = discoverChannels(mergedRows);
    // Live buffer alone can still carry channels that the windowed history lost.
    return live ? mergeChannelLists(fromMerge, discoverChannels(liveRows)) : fromMerge;
  }, [mergedRows, live, liveRows]);

  // Remember every channel seen this session so rolling Last 15/30 min windows
  // do not remove sensors that only appear on some ticks.
  const [knownChannels, setKnownChannels] = useState<string[]>([]);
  useEffect(() => {
    setKnownChannels([]);
  }, [tenant, gh]);
  useEffect(() => {
    setKnownChannels((prev) => {
      const next = mergeChannelLists(prev, discoveredNow);
      if (next.length === prev.length && next.every((ch, i) => ch === prev[i])) return prev;
      return next;
    });
  }, [discoveredNow]);

  const channels = useMemo(() => {
    const discovered = knownChannels.length > 0 ? knownChannels : discoveredNow;
    if (!channelFilter || channelFilter.length === 0) return discovered;
    if (channelMatch === "exact") {
      const wanted = new Set(channelFilter);
      return discovered.filter((ch) => wanted.has(ch));
    }
    return discovered.filter((ch) =>
      channelFilter.some((f) => ch.toLowerCase().includes(f.toLowerCase())),
    );
  }, [knownChannels, discoveredNow, channelFilter, channelMatch]);

  const [hidden, setHidden] = useState<ReadonlySet<string> | null>(null);
  const userToggledChannels = useRef(false);
  // New tenant/gh/jump focus clears manual toggles so preselect can re-apply.
  useEffect(() => {
    userToggledChannels.current = false;
    setHidden(null);
  }, [tenant, gh, channelParam]);

  // Anomaly jump: keep affected sensors from ``channel=`` pre-selected (and
  // others off) until the user toggles a channel themselves.
  useEffect(() => {
    if (!channelParam || channels.length === 0 || userToggledChannels.current) return;
    const matched = matchChannelParam(channels, channelParam);
    if (matched.length === 0) return;
    const matchedSet = new Set(matched);
    setHidden(new Set(channels.filter((ch) => !matchedSet.has(ch))));
  }, [channelParam, channels]);

  const jumpFocus = useMemo(
    () => (channelParam ? matchChannelParam(channels, channelParam) : []),
    [channelParam, channels],
  );

  const visible = useMemo(() => {
    if (hidden !== null) return new Set(channels.filter((ch) => !hidden.has(ch)));
    if (jumpFocus.length > 0) return new Set(jumpFocus);
    return new Set(channels);
  }, [channels, hidden, jumpFocus]);

  const paintAnomalyHighlight = shouldPaintAnomalyHighlight({
    channelParam,
    pageChannels: channels,
    visible,
  });
  const anomalyFocusVisible = useMemo(
    () => (jumpFocus.length > 0 ? jumpFocus.filter((ch) => visible.has(ch)) : undefined),
    [jumpFocus, visible],
  );

  function toggleChannel(ch: string): void {
    userToggledChannels.current = true;
    setHidden(() => {
      const next = new Set(channels.filter((candidate) => !visible.has(candidate)));
      if (next.has(ch)) next.delete(ch);
      else next.add(ch);
      return next;
    });
    // Profile after scheduling state so the paint path is not delayed by the probe.
    measureInteraction("ui.channel_toggle", { channel: ch });
  }

  if (isError) {
    return (
      <div className="mx-auto max-w-6xl space-y-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">{title}</h1>
            <p className="text-sm text-text-muted tabular-nums">{windowLabel}</p>
          </div>
          <SharedViewControls
            channels={channels}
            setpoints={setpoints}
            onUpdateSetpoint={updateSetpoint}
            onResetSetpoints={resetToDefaults}
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
          channels={channels}
          setpoints={setpoints}
          onUpdateSetpoint={updateSetpoint}
          onResetSetpoints={resetToDefaults}
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
