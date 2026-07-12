"use client";

import { useMemo } from "react";

import { UPlotChart } from "@/components/charts/UPlotChart";
import type { SensorRow } from "@/features/monitoring/api/readings";
import { channelColor } from "@/lib/colors";

interface SensorTimeSeriesChartProps {
  readonly rows: SensorRow[];
  readonly channels: string[];
  /** Channel visibility — toggled client-side, no refetch (<300 ms budget). */
  readonly visible: ReadonlySet<string>;
}

export function SensorTimeSeriesChart({ rows, channels, visible }: SensorTimeSeriesChartProps) {
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
    }),
    [channels, visible],
  );

  if (rows.length === 0) return null;
  return <UPlotChart data={data} options={options} height={320} />;
}
