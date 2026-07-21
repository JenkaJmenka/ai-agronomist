"use client";

import { parseAsBoolean, useQueryState } from "nuqs";

import { Button } from "@/components/ui/Button";
import { resumeLiveScope } from "@/features/monitoring/api/live";
import { DEFAULT_PERIOD_ID, periodHours, windowForPeriod } from "@/lib/periodPresets";
import { useDateRange } from "@/lib/useDateRange";
import { useScope } from "@/lib/useScope";

/**
 * "Go Live" toggle — drives the `live` URL param.
 * Enabling live resumes a scope muted by Clear all data, then streams.
 * When enabling without a window, applies the default 24h lookback so
 * period + refresh controls drive the rolling dashboard window.
 */
export function LiveToggle() {
  const [live, setLive] = useQueryState("live", parseAsBoolean.withDefault(false));
  const [{ from, to }, setRange] = useDateRange();
  const { tenant, gh } = useScope();

  async function handleClick(): Promise<void> {
    const next = !live;
    if (next) {
      if (from === null || to === null) {
        const hours = periodHours(DEFAULT_PERIOD_ID) ?? 24;
        await setRange(windowForPeriod(hours));
      }
      // Explicit resume after Clear all — SSE reconnect alone must not re-seed.
      try {
        await resumeLiveScope({ tenant, gh });
      } catch {
        // Still flip live on; stream may work if scope was never muted.
      }
    }
    await setLive(next);
  }

  return (
    <Button
      variant={live ? "primary" : "secondary"}
      size="sm"
      onClick={() => void handleClick()}
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
