"use client";

import { zoomOutWindow } from "@/components/charts/timeWindow";
import { Button } from "@/components/ui/Button";
import { useDateRange } from "@/lib/useDateRange";

interface ChartZoomOutButtonProps {
  readonly testId?: string;
}

/**
 * Expands the URL from/to window 2× about its midpoint (undo drag-zoom).
 * Hidden when no explicit window is set.
 */
export function ChartZoomOutButton({ testId = "chart-zoom-out" }: ChartZoomOutButtonProps) {
  const [{ from, to }, setRange] = useDateRange();

  if (from === null || to === null) return null;

  function handleZoomOut(): void {
    if (from === null || to === null) return;
    void setRange(zoomOutWindow({ from, to }));
  }

  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      onClick={handleZoomOut}
      data-testid={testId}
      title="Widen the time window (also: double-click the chart)"
    >
      Zoom out
    </Button>
  );
}
