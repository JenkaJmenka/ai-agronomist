/** Resolve the window synthetic injection should target (plan: inject-window-anchoring). */

import { DEFAULT_PERIOD_ID, periodHours } from "@/lib/periodPresets";

/**
 * Span of the injection window, in hours. Matches the FE's default view
 * period (`DEFAULT_PERIOD_ID`, `web/lib/periodPresets.ts:21` — backed by
 * `READINGS_RECENT_HOURS`, `src/config.py:443`), not the backend's injection
 * default (`SYNTHETIC_DEFAULT_WINDOW_HOURS`): anchoring alone isn't enough —
 * injecting a wider span than the view shows still leaves part of the
 * injected run outside the window the operator lands on. The `?? 24`
 * fallback is defensive only; `DEFAULT_PERIOD_ID` ("24h") is always present
 * in `PERIOD_PRESETS`, so it is unreachable today — kept because
 * `periodHours` takes an arbitrary string and TS can't prove the lookup
 * succeeds.
 */
export const INJECT_WINDOW_SPAN_HOURS = periodHours(DEFAULT_PERIOD_ID) ?? 24;

export interface InjectWindow {
  readonly windowStart: string;
  readonly windowEnd: string;
}

/**
 * Pick the window injected rows should land in so the operator actually sees
 * them, instead of the backend's wall-clock default
 * (`src/api/services/synthetic_anomalies.py:_resolve_window`).
 *
 * Anchors to the newest stored reading, falling back to wall-clock `now` only
 * when the scope has no stored data yet. Deliberately does NOT branch on the
 * Live toggle: that reads the `live` URL query param
 * (`web/features/monitoring/MonitoringView.tsx`), which reflects whether the
 * operator is watching the live view, not whether the scope's freshness state
 * is "live" server-side — those diverge whenever a scope keeps streaming
 * after boot-resume (migration 028) with nobody pressing Go Live, which would
 * anchor injection to wall-clock `now` while the still-closed Monitoring view
 * anchors to `max_timestamp`, reopening the same invisible-injection bug this
 * function exists to close.
 */
export function resolveInjectWindow(params: {
  readonly maxTimestamp: Date | null;
  readonly now?: Date;
}): InjectWindow {
  const windowEnd = params.maxTimestamp ?? params.now ?? new Date();
  const windowStart = new Date(windowEnd.getTime() - INJECT_WINDOW_SPAN_HOURS * 3_600_000);
  return {
    windowStart: windowStart.toISOString(),
    windowEnd: windowEnd.toISOString(),
  };
}
