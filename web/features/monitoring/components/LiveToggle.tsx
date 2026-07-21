"use client";

import { parseAsBoolean, useQueryState } from "nuqs";

import { Button } from "@/components/ui/Button";
import { DEFAULT_PERIOD_ID, periodHours, windowForPeriod } from "@/lib/periodPresets";
import { useDateRange } from "@/lib/useDateRange";

/**
 * "Go Live" toggle — drives the `live` URL param.
 * When enabling live without a window, applies the default 24h lookback so
 * period + refresh controls drive the rolling dashboard window.
 */
export function LiveToggle() {
  const [live, setLive] = useQueryState("live", parseAsBoolean.withDefault(false));
  const [{ from, to }, setRange] = useDateRange();

  function handleClick(): void {
    const next = !live;
    if (next && (from === null || to === null)) {
      const hours = periodHours(DEFAULT_PERIOD_ID) ?? 24;
      void setRange(windowForPeriod(hours));
    }
    void setLive(next);
  }

  return (
    <Button
      variant={live ? "primary" : "secondary"}
      size="sm"
      onClick={handleClick}
      aria-pressed={live}
      data-testid="live-toggle"
      data-live={live ? "true" : "false"}
    >
      <span
        className={`inline-block h-2 w-2 rounded-full ${
          live ? "animate-pulse bg-green-400" : "bg-text-muted"
        }`}
        aria-hidden="true"
      />
      Go Live
    </Button>
  );
}
