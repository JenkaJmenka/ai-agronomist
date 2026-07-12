"use client";

import { useMemo } from "react";

import { UPlotChart } from "@/components/charts/UPlotChart";
import type { SensorRow } from "@/features/monitoring/api/readings";

interface ScoreTimelineProps {
  readonly rows: SensorRow[];
  readonly threshold: number;
}

/** Anomaly-score series with the threshold as a reference line. */
export function ScoreTimeline({ rows, threshold }: ScoreTimelineProps) {
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
      scales: {
        y: {
          // Fit the data, never clip peaks; keep the threshold visible too.
          range: [0, Math.max(peak * 1.1, threshold * 1.4, 0.25)] as [number, number],
        },
      },
    }),
    [threshold, peak],
  );

  if (rows.length === 0) return null;
  return <UPlotChart data={data} options={options} height={260} />;
}
