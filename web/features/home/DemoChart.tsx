"use client";

import { useEffect, useMemo, useState } from "react";

import { UPlotChart } from "@/components/charts/UPlotChart";

const POINTS = 288; // one day at 5-minute cadence

function buildDemoSeries(nowSec: number): [number[], number[]] {
  const xs = new Array<number>(POINTS);
  const ys = new Array<number>(POINTS);
  for (let i = 0; i < POINTS; i++) {
    xs[i] = nowSec - (POINTS - i) * 300;
    ys[i] = 22 + 4 * Math.sin((i / POINTS) * 2 * Math.PI);
  }
  return [xs, ys];
}

/** Synthetic diurnal temperature curve proving the chart pipeline works. */
export function DemoChart() {
  // Defer wall-clock samples until mount so SSR HTML matches the first client paint.
  const [data, setData] = useState<[number[], number[]] | null>(null);
  useEffect(() => {
    setData(buildDemoSeries(Math.floor(Date.now() / 1000)));
  }, []);

  const options = useMemo(
    () => ({
      series: [{}, { label: "Temp °C", stroke: "#16a34a", width: 1.5 }],
      axes: [{}, { label: "°C" }],
      legend: { show: false },
    }),
    [],
  );

  if (data === null) {
    return <div data-testid="uplot-chart" className="h-[280px]" aria-hidden />;
  }
  return <UPlotChart data={data} options={options} />;
}
