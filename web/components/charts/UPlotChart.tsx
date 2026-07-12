"use client";

import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

interface UPlotChartProps {
  readonly data: uPlot.AlignedData;
  readonly options: Omit<uPlot.Options, "width" | "height">;
  readonly height?: number;
}

/**
 * Client-only uPlot wrapper (plan §3): canvas rendering, ResizeObserver-aware
 * width, imperative chart instance kept out of React state.
 */
export function UPlotChart({ data, options, height = 280 }: UPlotChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<uPlot | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = new uPlot({ ...options, width: container.clientWidth, height }, data, container);
    chartRef.current = chart;

    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) chart.setSize({ width, height });
    });
    observer.observe(container);

    return () => {
      observer.disconnect();
      chart.destroy();
      chartRef.current = null;
    };
  }, [data, options, height]);

  return <div ref={containerRef} data-testid="uplot-chart" />;
}
