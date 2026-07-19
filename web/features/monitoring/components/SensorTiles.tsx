"use client";

import { useMemo } from "react";

import { Skeleton } from "@/components/ui/Skeleton";
import type { SensorRow } from "@/features/monitoring/api/readings";
import { SensorTile } from "@/features/monitoring/components/SensorTile";
import type { Setpoints } from "@/features/monitoring/hooks/useSetpoints";
import { CHANNEL_META, getChannelStatus } from "@/features/monitoring/lib/channelMeta";

interface SensorTilesProps {
  /** All available rows (historical + live combined). */
  readonly rows: SensorRow[];
  /** Discovered channel names in stable order. */
  readonly channels: string[];
  /** Effective setpoints (defaults merged with user overrides). */
  readonly setpoints: Setpoints;
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
 */
export function SensorTiles({
  rows,
  channels,
  setpoints,
  isLive,
  isStale,
  isLoading,
}: SensorTilesProps) {
  // Pre-compute latest value + status per channel so stable memo deps.
  const tileData = useMemo(
    () =>
      channels.map((ch) => {
        const value = getLatestValue(rows, ch);
        const status = value !== null ? getChannelStatus(ch, value, setpoints) : ("ok" as const);
        return { channel: ch, value, status };
      }),
    [rows, channels, setpoints],
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
      {tileData.map(({ channel, value, status }) => (
        <SensorTile
          key={channel}
          channel={channel}
          value={value}
          meta={CHANNEL_META[channel]}
          status={status}
          isLive={isLive}
          isStale={isStale}
        />
      ))}
    </div>
  );
}
