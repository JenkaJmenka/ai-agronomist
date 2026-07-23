"use client";

import { useMemo, useRef } from "react";
import type uPlot from "uplot";

import { ChartResetButton } from "@/components/charts/ChartResetButton";
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
  /** Drag-selection / pan / zoom / reset update the URL window. */
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

  const peakRef = useRef(peak);
  peakRef.current = peak;
  const thresholdRef = useRef(threshold);
  thresholdRef.current = threshold;
  const onRangeSelectRef = useRef(onRangeSelect);
  onRangeSelectRef.current = onRangeSelect;
  const canSelectRange = Boolean(onRangeSelect);

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
          label: "Threshold",
          stroke: "#ef4444",
          width: 1,
          dash: [6, 4],
          points: { show: false },
        },
      ],
      legend: { show: false },
      ...(canSelectRange ? { cursor: SELECT_ONLY_CURSOR } : {}),
      ...(canSelectRange
        ? {
            hooks: {
              setSelect: [
                createRangeSelectHook((from, to) => {
                  onRangeSelectRef.current?.(from, to);
                }),
              ],
            },
          }
        : {}),
      scales: {
        y: {
          // Fit the data, never clip peaks; keep the threshold visible too.
          range: (): [number, number] => {
            const p = peakRef.current;
            const t = thresholdRef.current;
            return [0, Math.max(p * 1.1, t * 1.4, 0.25)];
          },
        },
      },
    }),
    [canSelectRange],
  );

  if (rows.length === 0) return null;
  return (
    <div className="space-y-2">
      {onRangeSelect ? (
        <div className="flex justify-end">
          <ChartResetButton />
        </div>
      ) : null}
      <UPlotChart data={data} options={options} height={260} onWindowChange={onRangeSelect} />
    </div>
  );
}
