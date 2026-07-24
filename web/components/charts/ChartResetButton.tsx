"use client";

import { useEffect } from "react";

import { ensureChartBaseline, readChartBaseline } from "@/components/charts/chartBaseline";
import { Button } from "@/components/ui/Button";
import { useDateRange } from "@/lib/useDateRange";

interface ChartResetButtonProps {
  /**
   * Scope key (e.g. `"monitoring:acme:gh1"`) — identifies which baseline to
   * read/write so Reset never restores another page's or scope's window.
   */
  readonly scopeKey: string;
  readonly testId?: string;
}

/**
 * Restores the chart URL window to the last Period / custom selection
 * (baseline), undoing drag-select and scroll zoom.
 */
export function ChartResetButton({ scopeKey, testId = "chart-reset" }: ChartResetButtonProps) {
  const [{ from, to }, setRange] = useDateRange();

  useEffect(() => {
    if (from === null || to === null) return;
    ensureChartBaseline(from, to, scopeKey);
  }, [from, to, scopeKey]);

  if (from === null || to === null) return null;

  function handleReset(): void {
    const baseline = readChartBaseline(scopeKey);
    if (baseline === null) return;
    void setRange(baseline);
  }

  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      onClick={handleReset}
      data-testid={testId}
      title="Reset to the selected time window (also: double-click the chart)"
    >
      Reset
    </Button>
  );
}
