"use client";

import { parseAsBoolean, useQueryState } from "nuqs";
import { rememberChartBaseline } from "@/components/charts/chartBaseline";
import { Button } from "@/components/ui/Button";
import { resumeLiveScope } from "@/features/monitoring/api/live";
import { fetchReadingsBounds } from "@/features/monitoring/api/readings";
import { rememberBoundsMax } from "@/features/monitoring/lib/boundsCache";
import {
  DEFAULT_PERIOD_ID,
  matchPeriodId,
  periodHours,
  periodWindowEnd,
  windowForPeriod,
} from "@/lib/periodPresets";
import { useDateRange } from "@/lib/useDateRange";
import { useScope } from "@/lib/useScope";

interface LiveToggleProps {
  /**
   * Page identifier (e.g. `"monitoring"`, `"climate"`).
   * Combined with tenant/gh to scope the chart baseline per page and scope.
   */
  readonly pageId: string;
}

/**
 * Live toggle — drives the `live` URL param.
 * Enabling live resumes a scope muted by Clear all data, then streams.
 * Always re-snaps the current preset (or default 24h) to an end anchor of
 * max(now, newest stored reading) so the chart matches the Period control.
 */
export function LiveToggle({ pageId }: LiveToggleProps) {
  const [live, setLive] = useQueryState("live", parseAsBoolean.withDefault(false));
  const [{ from, to }, setRange] = useDateRange();
  const { tenant, gh } = useScope();
  const scopeKey = `${pageId}:${tenant}:${gh}`;

  async function handleClick(): Promise<void> {
    const next = !live;
    if (!next) {
      await setLive(false);
      return;
    }

    const matched = from && to ? matchPeriodId(from, to) : DEFAULT_PERIOD_ID;
    const hours =
      periodHours(matched === "custom" ? DEFAULT_PERIOD_ID : matched) ??
      periodHours(DEFAULT_PERIOD_ID) ??
      24;

    // Flip live + optimistic wall-clock window immediately so SSE ticks land
    // inside the lookback (never anchor to a future historical CSV max).
    await setLive(true);
    const optimisticEnd = periodWindowEnd({ maxTimestamp: null, live: true });
    const optimistic = windowForPeriod(hours, optimisticEnd);
    rememberChartBaseline(optimistic.from, optimistic.to, scopeKey);
    await setRange(optimistic);

    try {
      const bounds = await fetchReadingsBounds({ tenant, gh });
      rememberBoundsMax(tenant, gh, bounds.max_timestamp);
      // Live end stays wall-clock regardless of stored max.
      const end = periodWindowEnd({ maxTimestamp: null, live: true });
      const next = windowForPeriod(hours, end);
      rememberChartBaseline(next.from, next.to, scopeKey);
      await setRange(next);
    } catch {
      // Keep the optimistic wall-clock window.
    }

    // Explicit resume after Clear all — SSE reconnect alone must not re-seed.
    try {
      await resumeLiveScope({ tenant, gh });
    } catch {
      // Stream may still work if scope was never muted.
    }
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
        className={`inline-block h-2 w-2 shrink-0 rounded-full ${
          live ? "animate-pulse bg-green-400" : "bg-text-muted"
        }`}
        aria-hidden="true"
      />
      Live
    </Button>
  );
}
