"use client";

import { useEffect, useRef } from "react";
import type { ChannelMeta, TileSetpointState } from "@/features/monitoring/lib/channelMeta";
import { formatSensorValue, getChannelLabel } from "@/features/monitoring/lib/channelMeta";

interface SensorTileProps {
  readonly channel: string;
  readonly value: number | null;
  readonly meta: ChannelMeta | undefined;
  /** Latest value vs setpoint range — drives data-setpoint-state only. */
  readonly status: TileSetpointState;
  /**
   * Detector-flagged readings for this channel in the current window.
   * Anomaly presence is the DOMINANT visual signal — it overrides the setpoint
   * colour and makes the tile red regardless of whether the value is in range.
   */
  readonly anomalyCount?: number;
  /** True while the live SSE stream is active (drives pulse animation). */
  readonly isLive: boolean;
  /** True when the stream is stale — dims the tile to indicate stalled data. */
  readonly isStale: boolean;
  /**
   * Short directional hint shown when the tile is out of setpoint range but
   * has NO detector anomaly (e.g. "below setpoint 53–77").  `null` or absent
   * when the value is within range or there is no range configured.
   */
  readonly setpointHint?: string | null;
  /**
   * Channel name of a physically related channel that HAS detector anomalies
   * on the current page.  Only set when this tile has no anomaly but is out of
   * setpoint range — it never adds a badge or count to this tile.
   */
  readonly relatedAnomalyChannel?: string | null;
}

/**
 * Individual sensor tile: shows the latest value for one channel with
 * status-based accent colouring and a one-shot glow pulse on live updates.
 *
 * Visual priority (highest first):
 *   1. Anomaly (anomalyCount > 0) → red border + red tint, regardless of setpoint
 *   2. Setpoint excursion, no anomaly → amber border + amber tint
 *   3. In range, no anomaly → green border
 *   4. No range / no data → neutral
 */
export function SensorTile({
  channel,
  value,
  meta,
  status,
  anomalyCount = 0,
  isLive,
  isStale,
  setpointHint,
  relatedAnomalyChannel,
}: SensorTileProps) {
  const prevValueRef = useRef<number | null>(null);
  const flashRef = useRef<HTMLDivElement>(null);

  // Trigger a one-shot glow pulse on the overlay div when the value changes
  // during live mode. DOM manipulation restarts the animation cleanly.
  useEffect(() => {
    if (!isLive || value === null || flashRef.current === null) return;
    if (prevValueRef.current === value) return;
    prevValueRef.current = value;

    const el = flashRef.current;
    el.classList.remove("tile-flash");
    // Force a synchronous layout so removing + re-adding the class restarts
    // the CSS animation (standard browser animation restart technique).
    el.getBoundingClientRect();
    el.classList.add("tile-flash");
  }, [isLive, value]);

  const label = meta?.label ?? channel;
  const unit = meta?.unit ?? "";
  const decimals = meta?.decimals ?? 2;

  // Dim when not in live mode (historical data) or when stream is stale.
  const isDimmed = !isLive || isStale;

  // ── Visual state derivation ────────────────────────────────────────────────
  // Anomaly is the dominant signal: even an in-range tile gets red treatment
  // when the detector flagged it (the product's core differentiator).
  const hasAnomaly = anomalyCount > 0;
  const isSetpointExcursion = !hasAnomaly && (status === "warning" || status === "critical");

  let borderClass: string;
  let bgClass: string;
  let valueColorClass: string;

  if (hasAnomaly) {
    borderClass = "border-l-sev-critical";
    bgClass = "bg-sev-critical/10";
    valueColorClass = "text-sev-critical";
  } else if (isSetpointExcursion) {
    borderClass = "border-l-sev-warning";
    bgClass = "bg-sev-warning/5";
    valueColorClass = "text-sev-warning";
  } else if (status === "ok") {
    borderClass = "border-l-sev-normal";
    bgClass = "bg-bg-elevated";
    valueColorClass = "text-text";
  } else {
    // no_range: no value yet or channel has no configured range
    borderClass = "border-l-border";
    bgClass = "bg-bg-elevated";
    valueColorClass = "text-text";
  }

  // Show the related-anomaly hint only when this tile is a setpoint excursion
  // (no anomaly of its own) and a coupled channel has anomalies.
  const showRelatedHint = isSetpointExcursion && relatedAnomalyChannel != null;
  const relatedLabel = relatedAnomalyChannel ? getChannelLabel(relatedAnomalyChannel) : null;

  return (
    <div
      data-testid={`sensor-tile-${channel}`}
      data-setpoint-state={status}
      data-anomaly-count={anomalyCount}
      {...(showRelatedHint && relatedAnomalyChannel
        ? { "data-related-anomaly": relatedAnomalyChannel }
        : {})}
      className={[
        "relative flex min-w-[7rem] flex-col gap-1.5 rounded-[var(--radius-card)]",
        "border border-border border-l-4 p-3 shadow-[var(--shadow-elevation)]",
        borderClass,
        bgClass,
        isDimmed ? "opacity-60" : "opacity-100",
        "transition-opacity duration-300",
      ].join(" ")}
    >
      {/*
       * Transparent overlay that receives the tile-flash animation.
       * Keeps the flash separate from the tile's own background so
       * status-based colouring is unaffected.
       */}
      <div
        ref={flashRef}
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 rounded-[var(--radius-card)]"
      />

      {/* Big numeric value + unit */}
      <span className={`text-2xl font-bold tabular-nums leading-none ${valueColorClass}`}>
        {value !== null ? formatSensorValue(value, decimals) : "—"}
        {value !== null && unit !== "" ? (
          <span className="ml-0.5 text-sm font-normal text-text-muted">{unit}</span>
        ) : null}
      </span>

      {/* Channel label + provenance / anomaly badges */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="text-xs text-text-muted">{label}</span>
        {anomalyCount > 0 ? (
          <span
            data-testid={`sensor-tile-anomaly-${channel}`}
            title="Detector-flagged readings for this sensor in the current window"
            className="rounded bg-sev-critical/15 px-1 py-px text-[10px] font-semibold leading-tight text-sev-critical"
          >
            {anomalyCount} {anomalyCount === 1 ? "anomaly" : "anomalies"}
          </span>
        ) : null}
        {meta?.provenance === "derived" ? (
          <span
            title="Derived from other sensor channels — not directly measured"
            className="rounded bg-border px-1 py-px text-[10px] leading-tight text-text-muted"
          >
            derived
          </span>
        ) : null}
      </div>

      {/* Setpoint direction hint — shown only for excursions without a detector anomaly */}
      {isSetpointExcursion && setpointHint ? (
        <span
          data-testid={`sensor-tile-setpoint-hint-${channel}`}
          className="text-[10px] leading-tight text-sev-warning"
        >
          {setpointHint}
        </span>
      ) : null}

      {/* Related-anomaly hint — coupled channel has anomalies while this tile has none */}
      {showRelatedHint ? (
        <span
          data-testid={`sensor-tile-related-${channel}`}
          className="text-[10px] leading-tight text-text-muted"
        >
          related to {relatedLabel} anomaly
        </span>
      ) : null}
    </div>
  );
}
