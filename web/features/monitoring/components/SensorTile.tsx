"use client";

import { useEffect, useRef } from "react";

import type { ChannelMeta, ChannelStatus } from "@/features/monitoring/lib/channelMeta";
import { formatSensorValue } from "@/features/monitoring/lib/channelMeta";

interface SensorTileProps {
  readonly channel: string;
  readonly value: number | null;
  readonly meta: ChannelMeta | undefined;
  readonly status: ChannelStatus;
  /** True while the live SSE stream is active (drives pulse animation). */
  readonly isLive: boolean;
  /** True when the stream is stale — dims the tile to indicate stalled data. */
  readonly isStale: boolean;
}

const STATUS_BORDER: Record<ChannelStatus, string> = {
  ok: "border-l-sev-normal",
  warning: "border-l-sev-warning",
  critical: "border-l-sev-critical",
};

const STATUS_VALUE_COLOR: Record<ChannelStatus, string> = {
  ok: "text-text",
  warning: "text-sev-warning",
  critical: "text-sev-critical",
};

const STATUS_BG: Record<ChannelStatus, string> = {
  ok: "bg-bg-elevated",
  warning: "bg-sev-warning/5",
  critical: "bg-sev-critical/5",
};

/**
 * Individual sensor tile: shows the latest value for one channel with
 * status-based accent colouring and a one-shot glow pulse on live updates.
 */
export function SensorTile({ channel, value, meta, status, isLive, isStale }: SensorTileProps) {
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

  return (
    <div
      data-testid={`sensor-tile-${channel}`}
      className={[
        "relative flex min-w-[7rem] flex-col gap-1.5 rounded-[var(--radius-card)]",
        "border border-border border-l-4 p-3 shadow-[var(--shadow-elevation)]",
        STATUS_BORDER[status],
        STATUS_BG[status],
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
      <span
        className={`text-2xl font-bold tabular-nums leading-none ${STATUS_VALUE_COLOR[status]}`}
      >
        {value !== null ? formatSensorValue(value, decimals) : "—"}
        {value !== null && unit !== "" ? (
          <span className="ml-0.5 text-sm font-normal text-text-muted">{unit}</span>
        ) : null}
      </span>

      {/* Channel label + provenance badge */}
      <div className="flex items-center gap-1">
        <span className="text-xs text-text-muted">{label}</span>
        {meta?.provenance === "derived" ? (
          <span
            title="Derived from other sensor channels — not directly measured"
            className="rounded bg-border px-1 py-px text-[10px] leading-tight text-text-muted"
          >
            derived
          </span>
        ) : null}
      </div>
    </div>
  );
}
