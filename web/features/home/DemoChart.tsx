"use client";

import { useMemo } from "react";

import { UPlotChart } from "@/components/charts/UPlotChart";

const POINTS = 288; // one day at 5-minute cadence

/** Synthetic diurnal temperature curve proving the chart pipeline works. */
export function DemoChart() {
  const data = useMemo(() => {
    const now = Math.floor(Date.now() / 1000);
    const xs = new Array<number>(POINTS);
    const ys = new Array<number>(POINTS);
    for (let i = 0; i < POINTS; i++) {
      xs[i] = now - (POINTS - i) * 300;
      ys[i] = 22 + 4 * Math.sin((i / POINTS) * 2 * Math.PI) + Math.random() * 0.4;
    }
    return [xs, ys] as [number[], number[]];
  }, []);

  const options = useMemo(
    () => ({
      series: [{}, { label: "Temp °C", stroke: "#16a34a", width: 1.5 }],
      axes: [{}, { label: "°C" }],
      legend: { show: false },
    }),
    [],
  );

  return <UPlotChart data={data} options={options} />;
}
