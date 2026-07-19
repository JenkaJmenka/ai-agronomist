"use client";

import { parseAsBoolean, parseAsFloat, parseAsString, useQueryState } from "nuqs";
import { Suspense, useEffect, useMemo, useState } from "react";

import { Card } from "@/components/ui/Card";
import { DateRangeControls } from "@/components/ui/DateRangeControls";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { discoverChannels, type SensorRow } from "@/features/monitoring/api/readings";
import { ChannelToggles } from "@/features/monitoring/components/ChannelToggles";
import { LiveAnomalyStrip } from "@/features/monitoring/components/LiveAnomalyStrip";
import { LiveBadge } from "@/features/monitoring/components/LiveBadge";
import { LiveToggle } from "@/features/monitoring/components/LiveToggle";
import { ReadingsTable } from "@/features/monitoring/components/ReadingsTable";
import { SensorTiles } from "@/features/monitoring/components/SensorTiles";
import { SensorTimeSeriesChart } from "@/features/monitoring/components/SensorTimeSeriesChart";
import { SetpointsPanel } from "@/features/monitoring/components/SetpointsPanel";
import { useLiveReadings } from "@/features/monitoring/hooks/useLiveReadings";
import { useReadings } from "@/features/monitoring/hooks/useReadings";
import { useSetpoints } from "@/features/monitoring/hooks/useSetpoints";
import { DEFAULT_SCORE_THRESHOLD } from "@/lib/severity";
import { useDateRange } from "@/lib/useDateRange";

interface MonitoringViewProps {
  readonly title: string;
  /** Restrict to these channels (e.g. gas page → co2). Empty = all discovered. */
  readonly channelFilter?: readonly string[];
}

function matchChannel(channels: string[], wanted: string): string[] {
  const exact = channels.filter((ch) => ch === wanted);
  if (exact.length > 0) return exact;
  return channels.filter((ch) => ch.toLowerCase().includes(wanted.toLowerCase()));
}

/** `channel=` carries a comma-separated list when an anomaly touches several sensors. */
function matchChannelParam(channels: string[], param: string): string[] {
  const wantedList = param
    .split(",")
    .map((w) => w.trim())
    .filter(Boolean);
  const matched = new Set(wantedList.flatMap((wanted) => matchChannel(channels, wanted)));
  return [...matched];
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

function MonitoringBody({ title, channelFilter }: MonitoringViewProps) {
  const {
    rows: historicalRows,
    isLoading,
    isRefetching,
    isError,
    error,
    windowLabel,
    downsampled,
  } = useReadings();

  const [live, setLive] = useQueryState("live", parseAsBoolean.withDefault(false));
  const [channelParam] = useQueryState("channel", parseAsString);
  const [threshold] = useQueryState("threshold", parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD));
  const [range, setRange] = useDateRange();

  // Setting a date window while live → force live off.
  useEffect(() => {
    if (live && range.from !== null && range.to !== null) {
      void setLive(false);
    }
  }, [live, range.from, range.to, setLive]);

  const { rows: liveRows, anomalies, transport, stale, sourceStatuses } = useLiveReadings(live);
  const { setpoints, updateSetpoint, resetToDefaults } = useSetpoints();

  const rows = useMemo(
    () => (live ? dedupeByTimestamp(historicalRows, liveRows) : historicalRows),
    [live, historicalRows, liveRows],
  );

  const channels = useMemo(() => {
    const discovered = discoverChannels(rows);
    if (!channelFilter || channelFilter.length === 0) return discovered;
    return discovered.filter((ch) =>
      channelFilter.some((f) => ch.toLowerCase().includes(f.toLowerCase())),
    );
  }, [rows, channelFilter]);

  const [hidden, setHidden] = useState<ReadonlySet<string> | null>(null);
  const visible = useMemo(() => {
    if (hidden !== null) return new Set(channels.filter((ch) => !hidden.has(ch)));
    if (channelParam) {
      const matched = matchChannelParam(channels, channelParam);
      if (matched.length > 0) return new Set(matched);
    }
    return new Set(channels);
  }, [channels, hidden, channelParam]);

  function toggleChannel(ch: string): void {
    setHidden(() => {
      const next = new Set(channels.filter((candidate) => !visible.has(candidate)));
      if (next.has(ch)) next.delete(ch);
      else next.add(ch);
      return next;
    });
  }

  if (isError) {
    throw error ?? new Error("Failed to load readings");
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
        <div className="flex items-center gap-2">
          <SetpointsPanel
            channels={channels}
            setpoints={setpoints}
            onUpdate={updateSetpoint}
            onReset={resetToDefaults}
          />
          <LiveToggle />
          {live && (
            <LiveBadge transport={transport} stale={stale} sourceStatuses={sourceStatuses} />
          )}
          <DateRangeControls />
        </div>
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
        <RefetchingBlock isRefetching={isRefetching} className="space-y-6">
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
              rows={rows}
              channels={channels}
              visible={visible}
              threshold={threshold}
              setpoints={setpoints}
              onRangeSelect={(from, to) => void setRange({ from, to })}
            />
          </Card>
          <Card title={`Readings (${rows.length.toLocaleString()} rows, newest first)`}>
            <ReadingsTable rows={rows} channels={channels} threshold={threshold} />
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
