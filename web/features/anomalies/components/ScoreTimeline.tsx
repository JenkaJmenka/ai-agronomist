"use client";

import { useMemo } from "react";
import type uPlot from "uplot";

import { ChartZoomOutButton } from "@/components/charts/ChartZoomOutButton";
import {
  createRangeSelectHook,
  type RangeSelectHandler,
  SELECT_ONLY_CURSOR,
} from "@/components/charts/rangeSelect";
import { UPlotChart } from "@/components/charts/UPlotChart";
import type { SensorRow } from "@/features/monitoring/api/readings";

interface ScoreTimelineProps {
  readonly rows: SensorRow[];
  readonly threshold: number;
  /** Drag-selection / pan / zoom-out update the URL window. */
  readonly onRangeSelect?: RangeSelectHandler;
}

/** Anomaly-score series with the threshold as a reference line. */
export function ScoreTimeline({ rows, threshold, onRangeSelect }: ScoreTimelineProps) {
  const data = useMemo(() => {
    const xs: number[] = [];
    const scores: (number | null)[] = [];
    const thresholdLine: number[] = [];
    for (const row of rows) {
      const score = row.anomaly_score;
      xs.push(new Date(row.timestamp).getTime() / 1000);
      scores.push(typeof score === "number" ? score : null);
      thresholdLine.push(threshold);
    }
    return [xs, scores, thresholdLine] as uPlot.AlignedData;
  }, [rows, threshold]);

  const peak = useMemo(() => {
    let max = 0;
    for (const row of rows) {
      const score = row.anomaly_score;
      if (typeof score === "number" && score > max) max = score;
    }
    return max;
  }, [rows]);

  const options = useMemo(
    () => ({
      series: [
        {},
        {
          label: "Anomaly score",
          // Canvas rendering — CSS variables do not resolve here; use the
          // literal severity hex values (tokens §7.1).
          stroke: "#ff8c00",
          width: 1.5,
          points: { show: false },
        },
        {
          label: `Threshold ${threshold.toFixed(3)}`,
          stroke: "#ef4444",
          width: 1,
          dash: [6, 4],
          points: { show: false },
        },
      ],
      legend: { show: false },
      ...(onRangeSelect ? { cursor: SELECT_ONLY_CURSOR } : {}),
      ...(onRangeSelect ? { hooks: { setSelect: [createRangeSelectHook(onRangeSelect)] } } : {}),
      scales: {
        y: {
          // Fit the data, never clip peaks; keep the threshold visible too.
          range: [0, Math.max(peak * 1.1, threshold * 1.4, 0.25)] as [number, number],
        },
      },
    }),
    [threshold, peak, onRangeSelect],
  );

  if (rows.length === 0) return null;
  return (
    <div className="space-y-2">
      {onRangeSelect ? (
        <div className="flex justify-end">
          <ChartZoomOutButton />
        </div>
      ) : null}
      <UPlotChart data={data} options={options} height={260} onWindowChange={onRangeSelect} />
    </div>
  );
}
