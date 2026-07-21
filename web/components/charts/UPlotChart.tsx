"use client";

import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

import { TIME_X_AXIS } from "@/components/charts/timeAxis";

interface UPlotChartProps {
  readonly data: uPlot.AlignedData;
  readonly options: Omit<uPlot.Options, "width" | "height">;
  readonly height?: number;
}

/** Merge shared time x-axis defaults; chart-specific axis fields still win. */
function withTimeAxis(
  options: Omit<uPlot.Options, "width" | "height">,
): Omit<uPlot.Options, "width" | "height"> {
  const incoming = options.axes ?? [{}, {}];
  const axes = incoming.map((axis, index) => {
    if (index !== 0) return axis ?? {};
    return {
      ...TIME_X_AXIS,
      ...axis,
      // Keep our denser-safe stamps unless the chart supplies its own values.
      values: axis?.values ?? TIME_X_AXIS.values,
      space: axis?.space ?? TIME_X_AXIS.space,
      gap: axis?.gap ?? TIME_X_AXIS.gap,
      size: axis?.size ?? TIME_X_AXIS.size,
    };
  });
  // Ensure an x-axis exists even when the chart omitted `axes` entirely.
  if (axes.length === 0) axes.push({ ...TIME_X_AXIS });
  return { ...options, axes };
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

    const merged = withTimeAxis(options);
    const chart = new uPlot({ ...merged, width: container.clientWidth, height }, data, container);
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
