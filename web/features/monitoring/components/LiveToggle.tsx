"use client";

import { parseAsBoolean, useQueryState } from "nuqs";
import { rememberChartBaseline } from "@/components/charts/chartBaseline";
import { Button } from "@/components/ui/Button";
import { pauseLiveScope, resumeLiveScope } from "@/features/monitoring/api/live";
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
 * Enabling live resumes the scope (muted on the backend by default and by
 * Clear all data) so it starts publishing/persisting again, then streams.
 * Disabling live pauses the scope so mock-SCADA rows stop landing in the DB
 * the moment the operator turns Live off.
 * Always re-snaps the current preset (or default 24h) to an end anchor of
 * max(now, newest stored reading) so the chart matches the Period control.
 *
 * This is also the ONLY resume path on pages that render LiveToggle without
 * mounting useLiveReadings (Thresholds, Anomalies — via SharedViewControls);
 * MonitoringView/HomeView/LiveBadge/LiveAnomalyStrip get a second one-shot
 * resume from useLiveReadings for the reload-with-?live=true case, but this
 * click handler must still resume on those other pages by itself.
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
      // Tolerant like resumeLiveScope below — a failed pause must not block
      // the toggle flipping off; the backend also mutes on its own gates.
      try {
        await pauseLiveScope({ tenant, gh });
      } catch {
        // Scope may already be muted, or the request raced a page unload.
      }
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

    // Dispatch resume before anything else in this branch, but don't await
    // it — the round trip would delay the optimistic window snap and bounds
    // fetch below by a full request. Safety doesn't come from awaiting; it
    // comes from this request hitting the wire before any later toggle-off
    // click's pause request can, combined with the backend's asyncio.Lock
    // serializing start_scope/stop_scope. As long as resume is dispatched
    // first, last-click-wins holds at the server even though we move on
    // here before it resolves.
    resumeLiveScope({ tenant, gh }).catch(() => {
      // Stream may still work if the scope was never muted.
    });

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
