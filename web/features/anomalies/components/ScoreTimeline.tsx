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

  const options = useMemo(
    () => ({
      series: [
        {},
        {
          label: "Anomaly score",
          stroke: "var(--sev-lstm)",
          width: 1.5,
          points: { show: false },
        },
        {
          label: `Threshold ${threshold.toFixed(3)}`,
          stroke: "var(--sev-critical)",
          width: 1,
          dash: [6, 4],
          points: { show: false },
        },
      ],
      legend: { show: false },
      scales: { y: { range: [0, Math.max(threshold * 1.4, 0.25)] as [number, number] } },
    }),
    [threshold],
  );

  if (rows.length === 0) return null;
  return <UPlotChart data={data} options={options} height={260} />;
}
