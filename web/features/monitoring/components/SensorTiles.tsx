"use client";

import { useMemo } from "react";

import { Skeleton } from "@/components/ui/Skeleton";
import type { SensorRow } from "@/features/monitoring/api/readings";
import { SensorTile } from "@/features/monitoring/components/SensorTile";
import type { Setpoints } from "@/features/monitoring/hooks/useSetpoints";
import {
  CHANNEL_META,
  getChannelStatus,
  hasSetpointRange,
  type TileSetpointState,
} from "@/features/monitoring/lib/channelMeta";
import {
  formatSetpointHint,
  getRelatedAnomalyChannel,
} from "@/features/monitoring/lib/setpointContext";

interface SensorTilesProps {
  /** All available rows (historical + live combined). */
  readonly rows: SensorRow[];
  /** Discovered channel names in stable order. */
  readonly channels: string[];
  /** Effective setpoints (defaults merged with user overrides). */
  readonly setpoints: Setpoints;
  /** Detector-flagged readings per channel in the current window (independent of setpoints). */
  readonly anomalyCounts?: ReadonlyMap<string, number>;
  /** True when the live SSE stream is active. */
  readonly isLive: boolean;
  /** True when the stream hasn't delivered data for ≥30 s. */
  readonly isStale: boolean;
  /** True while the initial historical fetch is in-flight. */
  readonly isLoading: boolean;
}

/**
 * Scan rows newest-first to find the latest numeric value for `channel`.
 * Historical rows may lack some channels (e.g. soil_moisture added later).
 */
function getLatestValue(rows: SensorRow[], channel: string): number | null {
  for (let i = rows.length - 1; i >= 0; i--) {
    const v = rows[i]?.[channel];
    if (typeof v === "number") return v;
  }
  return null;
}

/**
 * Responsive tile row showing the latest value per discovered channel.
 *
 * Tiles appear above the time-series chart so operators can read the current
 * greenhouse state at a glance without scrolling to the chart.
 *
 * Visual priority (see SensorTile):
 *   1. Anomaly present → red (dominant signal)
 *   2. Setpoint excursion, no anomaly → amber (quieter treatment)
 *   3. In range, no anomaly → green
 *   4. No range / no data → neutral
 */
export function SensorTiles({
  rows,
  channels,
  setpoints,
  anomalyCounts,
  isLive,
  isStale,
  isLoading,
}: SensorTilesProps) {
  const effectiveCounts = anomalyCounts ?? (new Map() as ReadonlyMap<string, number>);

  // Pre-compute latest value + status per channel so stable memo deps.
  const tileData = useMemo(
    () =>
      channels.map((ch) => {
        const value = getLatestValue(rows, ch);
        // No range (or no value yet) → neutral, never green: "ok" must mean
        // "measured and inside a range", not "nothing to compare against".
        const status: TileSetpointState =
          value === null || !hasSetpointRange(ch, setpoints)
            ? "no_range"
            : getChannelStatus(ch, value, setpoints);
        const anomalyCount = effectiveCounts.get(ch) ?? 0;

        // Setpoint hint and related-anomaly hint are only meaningful for
        // tiles that are out of range AND have no detector anomaly.
        const isExcursionNoAnomaly =
          anomalyCount === 0 && (status === "warning" || status === "critical");

        const setpointHint = isExcursionNoAnomaly ? formatSetpointHint(ch, value, setpoints) : null;

        const relatedAnomalyChannel = isExcursionNoAnomaly
          ? getRelatedAnomalyChannel(ch, effectiveCounts)
          : null;

        return { channel: ch, value, status, anomalyCount, setpointHint, relatedAnomalyChannel };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rows, channels, setpoints, effectiveCounts],
  );

  if (isLoading) {
    return (
      <div data-testid="sensor-tiles-loading" className="flex flex-wrap gap-3">
        {[...Array(4)].map((_, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton count
          <Skeleton key={i} className="h-16 w-28" />
        ))}
      </div>
    );
  }

  if (channels.length === 0) {
    return (
      <p data-testid="sensor-tiles-empty" className="text-sm text-text-muted">
        No sensor channels detected yet.
      </p>
    );
  }

  return (
    <div data-testid="sensor-tiles" className="flex flex-wrap gap-3">
      {tileData.map(
        ({ channel, value, status, anomalyCount, setpointHint, relatedAnomalyChannel }) => (
          <SensorTile
            key={channel}
            channel={channel}
            value={value}
            meta={CHANNEL_META[channel]}
            status={status}
            anomalyCount={anomalyCount}
            isLive={isLive}
            isStale={isStale}
            setpointHint={setpointHint}
            relatedAnomalyChannel={relatedAnomalyChannel}
          />
        ),
      )}
    </div>
  );
}
