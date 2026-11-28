"use client";

import { useId, useState } from "react";

import { DriftSeriesCharts } from "@/features/drift-watch/components/DriftSeriesCharts";

/** Collapsed by default; the series is fetched only once the panel is opened. */
export function DriftInputChartPanel({ inputId }: { readonly inputId: string }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  return (
    <div className="mt-2 w-full">
      <button
        type="button"
        data-testid="chart-toggle"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className="text-sm font-medium text-accent hover:text-accent-hover"
      >
        {open ? "Hide chart" : "Chart"}
      </button>
      <div id={panelId} className="mt-2">
        {open ? <DriftSeriesCharts inputId={inputId} /> : null}
      </div>
    </div>
  );
}
