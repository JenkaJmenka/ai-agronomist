"use client";

import { useMemo } from "react";
import type uPlot from "uplot";

import {
  createRangeSelectHook,
  type RangeSelectHandler,
  SELECT_ONLY_CURSOR,
} from "@/components/charts/rangeSelect";
import { UPlotChart } from "@/components/charts/UPlotChart";
import type { SensorRow } from "@/features/monitoring/api/readings";
import { type AnomalyBand, computeAnomalyBands } from "@/features/monitoring/lib/anomalyBands";
import { ANOMALY_BAND_FILL, channelColor } from "@/lib/colors";

interface SensorTimeSeriesChartProps {
  readonly rows: SensorRow[];
  readonly channels: string[];
  /** Channel visibility — toggled client-side, no refetch (<300 ms budget). */
  readonly visible: ReadonlySet<string>;
  /** Rows scoring at/above this get a red background band on the chart. */
  readonly threshold: number;
  /** Drag-selection on the x-axis reports a [from, to] window. */
  readonly onRangeSelect?: RangeSelectHandler;
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

export function SensorTimeSeriesChart({
  rows,
  channels,
  visible,
  threshold,
  onRangeSelect,
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

  const bands = useMemo(() => computeAnomalyBands(rows, threshold), [rows, threshold]);

  const options = useMemo(
    () => ({
      series: [
        {},
        ...channels.map((ch, i) => ({
          label: ch,
          stroke: channelColor(i),
          width: 1.5,
          show: visible.has(ch),
        })),
      ],
      legend: { show: false },
      ...(onRangeSelect ? { cursor: SELECT_ONLY_CURSOR } : {}),
      // drawClear runs after the canvas reset and before series strokes,
      // so bands sit behind the traces.
      hooks: {
        drawClear: [(u: uPlot) => drawAnomalyBands(u, bands)],
        ...(onRangeSelect ? { setSelect: [createRangeSelectHook(onRangeSelect)] } : {}),
      },
    }),
    [channels, visible, bands, onRangeSelect],
  );

  if (rows.length === 0) return null;
  return <UPlotChart data={data} options={options} height={320} />;
}
