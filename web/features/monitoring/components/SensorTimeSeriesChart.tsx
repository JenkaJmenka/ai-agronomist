"use client";

import { useMemo, useRef } from "react";
import type uPlot from "uplot";

import { ChartResetButton } from "@/components/charts/ChartResetButton";
import {
  createRangeSelectHook,
  type RangeSelectHandler,
  SELECT_ONLY_CURSOR,
} from "@/components/charts/rangeSelect";
import { type ChartViewWindow, UPlotChart } from "@/components/charts/UPlotChart";
import type { SensorRow } from "@/features/monitoring/api/readings";
import type { Setpoints } from "@/features/monitoring/hooks/useSetpoints";
import { type AnomalyBand, computeAnomalyBands } from "@/features/monitoring/lib/anomalyBands";
import { ANOMALY_BAND_FILL, channelColor, OPTIMAL_BAND_FILL } from "@/lib/colors";

interface SensorTimeSeriesChartProps {
  readonly rows: SensorRow[];
  readonly channels: string[];
  /** Channel visibility — toggled client-side, no refetch (<300 ms budget). */
  readonly visible: ReadonlySet<string>;
  /** Rows scoring at/above this get a red background band on the chart. */
  readonly threshold: number;
  /** Drag-selection / pan / zoom / reset update the URL window. */
  readonly onRangeSelect?: RangeSelectHandler;
  /** Local chart zoom from table row selection (does not change the table). */
  readonly viewWindow?: ChartViewWindow | null;
  /**
   * Effective setpoints. When exactly one channel is visible its optimal
   * [min, max] range is drawn as a subtle horizontal green band.
   */
  readonly setpoints?: Setpoints;
  /** When false, skip anomaly time bands (jump sensors inactive / wrong page). */
  readonly showAnomalyBands?: boolean;
  /** Jump-focus channels currently visible — narrows which runs band. */
  readonly anomalyFocusVisible?: readonly string[];
  /** Detector-attributed sensors keyed by reading timestamp. */
  readonly affectedByTimestamp?: ReadonlyMap<string, readonly string[]>;
}

/** Paints translucent bands behind the series over anomalous time spans. */
function drawAnomalyBands(u: uPlot, bands: AnomalyBand[]): void {
  const { ctx, bbox } = u;
  ctx.save();
  ctx.fillStyle = ANOMALY_BAND_FILL;
  for (const [start, end] of bands) {
    const left = Math.max(u.valToPos(start, "x", true), bbox.left);
    const right = Math.min(u.valToPos(end, "x", true), bbox.left + bbox.width);
    if (right > left) ctx.fillRect(left, bbox.top, right - left, bbox.height);
  }
  ctx.restore();
}

/**
 * Paints a horizontal shaded band between the optimal [min, max] setpoints
 * for the y-axis. Only called when exactly one channel is visible so we know
 * the y-scale maps to a single sensor's value range.
 */
function drawOptimalBand(u: uPlot, min: number, max: number): void {
  const { ctx, bbox } = u;
  ctx.save();
  ctx.fillStyle = OPTIMAL_BAND_FILL;

  // In uPlot y increases downward in pixel space, so valToPos(max) < valToPos(min).
  const yTop = Math.max(u.valToPos(max, "y", true), bbox.top);
  const yBottom = Math.min(u.valToPos(min, "y", true), bbox.top + bbox.height);

  if (yBottom > yTop) {
    ctx.fillRect(bbox.left, yTop, bbox.width, yBottom - yTop);
  }
  ctx.restore();
}

export function SensorTimeSeriesChart({
  rows,
  channels,
  visible,
  threshold,
  onRangeSelect,
  viewWindow = null,
  setpoints,
  showAnomalyBands = true,
  anomalyFocusVisible,
  affectedByTimestamp,
}: SensorTimeSeriesChartProps) {
  const data = useMemo(() => {
    const xs = rows.map((r) => new Date(r.timestamp).getTime() / 1000);
    const series = channels.map((ch) =>
      rows.map((r) => {
        const v = r[ch];
        return typeof v === "number" ? v : null;
      }),
    );
    return [xs, ...series] as uPlot.AlignedData;
  }, [rows, channels]);

  const bands = useMemo(
    () =>
      computeAnomalyBands(rows, threshold, {
        enabled: showAnomalyBands,
        focusVisible: anomalyFocusVisible,
        affectedByTimestamp,
      }),
    [rows, threshold, showAnomalyBands, anomalyFocusVisible, affectedByTimestamp],
  );

  const optimalRange = useMemo((): readonly [number, number] | null => {
    if (setpoints === undefined) return null;
    const visibleChannels = channels.filter((ch) => visible.has(ch));
    if (visibleChannels.length !== 1) return null;
    const ch = visibleChannels[0];
    if (ch === undefined) return null;
    return setpoints[ch] ?? null;
  }, [channels, visible, setpoints]);

  // Keep draw/select hooks stable so live ticks do not rebuild uPlot options.
  const bandsRef = useRef(bands);
  bandsRef.current = bands;
  const optimalRangeRef = useRef(optimalRange);
  optimalRangeRef.current = optimalRange;
  const onRangeSelectRef = useRef(onRangeSelect);
  onRangeSelectRef.current = onRangeSelect;

  const channelsKey = channels.join("\0");
  const canSelectRange = Boolean(onRangeSelect);
  const seriesVisibility = useMemo(
    () => channels.map((ch) => visible.has(ch)),
    [channels, visible],
  );

  const options = useMemo(
    () => ({
      series: [
        {},
        ...channels.map((ch, i) => ({
          label: ch,
          stroke: channelColor(i),
          width: 1.5,
        })),
      ],
      legend: { show: false },
      ...(canSelectRange ? { cursor: SELECT_ONLY_CURSOR } : {}),
      hooks: {
        drawClear: [
          (u: uPlot) => {
            drawAnomalyBands(u, bandsRef.current);
            const optimal = optimalRangeRef.current;
            if (optimal !== null) {
              drawOptimalBand(u, optimal[0], optimal[1]);
            }
          },
        ],
        ...(canSelectRange
          ? {
              setSelect: [
                createRangeSelectHook((from, to) => {
                  onRangeSelectRef.current?.(from, to);
                }),
              ],
            }
          : {}),
      },
    }),
    // channelsKey keeps options stable when the channels array identity churns.
    // Visibility is applied via seriesVisibility → setSeries (not options).
    [channels, channelsKey, canSelectRange],
  );

  if (rows.length === 0) return null;
  return (
    <div className="space-y-2">
      {onRangeSelect ? (
        <div className="flex justify-end">
          <ChartResetButton />
        </div>
      ) : null}
      <UPlotChart
        data={data}
        options={options}
        height={320}
        onWindowChange={onRangeSelect}
        viewWindow={viewWindow}
        seriesVisibility={seriesVisibility}
      />
    </div>
  );
}
