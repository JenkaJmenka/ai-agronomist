"use client";

import { useCallback, useEffect, useState } from "react";

import type { ChartViewWindow } from "@/components/charts/UPlotChart";

/**
 * Local chart x-axis focus from table row selection.
 *
 * Does not write the URL period — the table keeps its current rows while the
 * chart zooms. Cleared when the period window (or scope) changes.
 */
export function useChartViewWindow(options: {
  readonly from: Date | null;
  readonly to: Date | null;
  /** Extra reset key (e.g. ``tenant/gh``) so jumps clear the focus. */
  readonly scopeKey?: string;
}): {
  readonly chartViewWindow: ChartViewWindow | null;
  readonly onTableRowSelect: (from: Date, to: Date) => void;
} {
  const { from, to, scopeKey = "" } = options;
  const [chartViewWindow, setChartViewWindow] = useState<ChartViewWindow | null>(null);

  useEffect(() => {
    setChartViewWindow(null);
  }, [from, to, scopeKey]);

  const onTableRowSelect = useCallback((nextFrom: Date, nextTo: Date) => {
    setChartViewWindow({ from: nextFrom, to: nextTo });
  }, []);

  return { chartViewWindow, onTableRowSelect };
}
