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
import { type AnomalyBand, computeAnomalyBands } from "@/features/monitoring/lib/anomalyBands";
import { ANOMALY_BAND_FILL, channelColor } from "@/lib/colors";

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
  /** When false, skip anomaly time bands (jump sensors inactive / wrong page). */
  readonly showAnomalyBands?: boolean;
  /** Jump-focus channels currently visible — narrows which runs band. */
  readonly anomalyFocusVisible?: readonly string[];
  /** Detector-attributed sensors keyed by reading timestamp. */
  readonly affectedByTimestamp?: ReadonlyMap<string, readonly string[]>;
  /**
   * Scope key for the chart baseline (e.g. `"monitoring:acme:gh1"`).
   * Passed to ChartResetButton and UPlotChart so Reset and double-click
   * never restore another page's or scope's window.
   */
  readonly scopeKey: string;
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
  viewWindow = null,
  showAnomalyBands = true,
  anomalyFocusVisible,
  affectedByTimestamp,
  scopeKey,
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

  // Keep draw/select hooks stable so live ticks do not rebuild uPlot options.
  const bandsRef = useRef(bands);
  bandsRef.current = bands;
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
          // Do not bridge multi-minute gaps — live lookback often has sparse
          // history and connecting them flattens the left side of the chart.
          spanGaps: false,
        })),
      ],
      legend: { show: false },
      ...(canSelectRange ? { cursor: SELECT_ONLY_CURSOR } : {}),
      hooks: {
        drawClear: [
          (u: uPlot) => {
            // Only anomaly time spans — never the full-height optimal setpoint
            // wash (light [0,1000] covered the entire plot).
            drawAnomalyBands(u, bandsRef.current);
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
          <ChartResetButton scopeKey={scopeKey} />
        </div>
      ) : null}
      <UPlotChart
        data={data}
        options={options}
        height={320}
        onWindowChange={onRangeSelect}
        chartBaselineScopeKey={scopeKey}
        viewWindow={viewWindow}
        seriesVisibility={seriesVisibility}
      />
    </div>
  );
}
