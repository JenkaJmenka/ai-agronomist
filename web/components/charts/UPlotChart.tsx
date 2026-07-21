"use client";

import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

import { TIME_X_AXIS } from "@/components/charts/timeAxis";
import { panWindow, scaleWindowSec, zoomOutWindow } from "@/components/charts/timeWindow";

export type ChartWindowHandler = (from: Date, to: Date) => void;

interface UPlotChartProps {
  readonly data: uPlot.AlignedData;
  readonly options: Omit<uPlot.Options, "width" | "height">;
  readonly height?: number;
  /**
   * When set, wheel pans the time window and double-click zooms out.
   * Callers typically write the new [from, to] into the URL.
   */
  readonly onWindowChange?: ChartWindowHandler;
}

/** Fraction of the visible span to shift per wheel notch. */
const PAN_FRACTION = 0.15;

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
      values: axis?.values ?? TIME_X_AXIS.values,
      space: axis?.space ?? TIME_X_AXIS.space,
      gap: axis?.gap ?? TIME_X_AXIS.gap,
      size: axis?.size ?? TIME_X_AXIS.size,
    };
  });
  if (axes.length === 0) axes.push({ ...TIME_X_AXIS });
  return { ...options, axes };
}

/**
 * Client-only uPlot wrapper (plan §3): canvas rendering, ResizeObserver-aware
 * width, imperative chart instance kept out of React state.
 *
 * Data updates call ``setData`` — the chart is only recreated when options or
 * height change. Recreating on every live tick froze the main thread and made
 * sidebar navigation feel dead after opening Monitoring/Climate/Gas.
 */
export function UPlotChart({ data, options, height = 280, onWindowChange }: UPlotChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<uPlot | null>(null);
  const onWindowChangeRef = useRef(onWindowChange);
  onWindowChangeRef.current = onWindowChange;
  const dataRef = useRef(data);
  dataRef.current = data;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const merged = withTimeAxis(options);
    const chart = new uPlot(
      { ...merged, width: container.clientWidth, height },
      dataRef.current,
      container,
    );
    chartRef.current = chart;

    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) chart.setSize({ width, height });
    });
    observer.observe(container);

    function currentWindow() {
      const u = chartRef.current;
      if (!u) return null;
      return scaleWindowSec(u.scales.x.min, u.scales.x.max);
    }

    let lastPanAt = 0;

    function handleWheel(event: WheelEvent): void {
      if (!onWindowChangeRef.current) return;
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      if (delta === 0) return;
      event.preventDefault();
      const now = Date.now();
      // Throttle URL updates — trackpads emit many events per gesture.
      if (now - lastPanAt < 100) return;
      lastPanAt = now;
      const win = currentWindow();
      if (!win) return;
      const next = panWindow(win, Math.sign(delta) * PAN_FRACTION);
      onWindowChangeRef.current(next.from, next.to);
    }

    function handleDblClick(event: MouseEvent): void {
      if (!onWindowChangeRef.current) return;
      event.preventDefault();
      const win = currentWindow();
      if (!win) return;
      const next = zoomOutWindow(win);
      onWindowChangeRef.current(next.from, next.to);
    }

    container.addEventListener("wheel", handleWheel, { passive: false });
    container.addEventListener("dblclick", handleDblClick);

    return () => {
      container.removeEventListener("wheel", handleWheel);
      container.removeEventListener("dblclick", handleDblClick);
      observer.disconnect();
      chart.destroy();
      chartRef.current = null;
    };
    // Intentionally omit ``data`` — live ticks must not destroy/recreate the chart.
  }, [options, height]);

  useEffect(() => {
    chartRef.current?.setData(data);
  }, [data]);

  return (
    <div
      ref={containerRef}
      data-testid="uplot-chart"
      className={onWindowChange ? "touch-pan-y" : undefined}
      title={
        onWindowChange ? "Scroll to pan · drag to zoom in · double-click to zoom out" : undefined
      }
    />
  );
}
