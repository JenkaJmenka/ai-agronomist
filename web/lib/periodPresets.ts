/** Shared period / refresh presets for monitoring window controls. */

export interface PeriodPreset {
  readonly id: string;
  readonly label: string;
  /** Lookback duration in hours (fractional for sub-hour windows). */
  readonly hours: number;
}

export const PERIOD_PRESETS: readonly PeriodPreset[] = [
  { id: "15m", label: "Last 15 min", hours: 0.25 },
  { id: "30m", label: "Last 30 min", hours: 0.5 },
  { id: "1h", label: "Last hour", hours: 1 },
  { id: "12h", label: "Last 12 hours", hours: 12 },
  { id: "24h", label: "Last 24 hours", hours: 24 },
  { id: "7d", label: "Last 7 days", hours: 24 * 7 },
  { id: "1mo", label: "Last 1 month", hours: 24 * 30 },
  { id: "6mo", label: "Last 6 months", hours: 24 * 182 },
] as const;

export const DEFAULT_PERIOD_ID = "24h";

export const REFRESH_PRESETS = [
  { id: "10", label: "10 sec", seconds: 10 },
  { id: "30", label: "30 sec", seconds: 30 },
  { id: "60", label: "1 min", seconds: 60 },
  { id: "300", label: "5 min", seconds: 300 },
] as const;

export const DEFAULT_REFRESH_SECONDS = 30;

export function periodHours(periodId: string): number | null {
  const preset = PERIOD_PRESETS.find((p) => p.id === periodId);
  return preset?.hours ?? null;
}

export function windowForPeriod(hours: number, end: Date = new Date()): { from: Date; to: Date } {
  return {
    from: new Date(end.getTime() - hours * 3_600_000),
    to: end,
  };
}

/**
 * Choose the end of a preset lookback.
 *
 * Historical uploads often sit far from wall-clock "now" — prefer the newest
 * stored timestamp so "Last 15/30 min" stays filled.
 *
 * While Live is on, always use wall-clock ``now``. Mock/live SCADA emits
 * wall-clock ticks; anchoring to a future historical max (demo CSV) clips
 * every incoming reading out of the chart window.
 */
export function periodWindowEnd(options: {
  readonly maxTimestamp: Date | null;
  readonly live: boolean;
  readonly now?: Date;
}): Date {
  const now = options.now ?? new Date();
  if (options.live) return now;
  return options.maxTimestamp ?? now;
}

/** Match a from/to span to a preset id when within ~2% tolerance; else "custom". */
export function matchPeriodId(from: Date | null, to: Date | null): string {
  if (!from || !to) return DEFAULT_PERIOD_ID;
  const hours = (to.getTime() - from.getTime()) / 3_600_000;
  if (hours <= 0) return "custom";
  for (const preset of PERIOD_PRESETS) {
    const tol = Math.max(preset.hours * 0.02, 1 / 60);
    if (Math.abs(hours - preset.hours) <= tol) return preset.id;
  }
  return "custom";
}

/** Keep only rows whose timestamps fall inside [from, to] (inclusive). */
export function filterRowsToWindow<T extends { readonly timestamp: string }>(
  rows: readonly T[],
  from: Date | null,
  to: Date | null,
): T[] {
  if (from === null || to === null) return [...rows];
  const startMs = from.getTime();
  const endMs = to.getTime();
  if (!(endMs >= startMs)) return [...rows];
  return rows.filter((row) => {
    const ms = Date.parse(row.timestamp);
    return Number.isFinite(ms) && ms >= startMs && ms <= endMs;
  });
}

/**
 * Chart/table rows for the active URL window.
 *
 * Always clip to ``from``/``to``. During refetch, keepPreviousData may still
 * hold a wider prior payload — clipping prevents the chart from flashing that
 * stale wider range (period-switch flicker). The RefetchingBlock dims the UI
 * until the fetch settles.
 */
export function rowsForActiveWindow<T extends { readonly timestamp: string }>(params: {
  readonly rows: readonly T[];
  readonly from: Date | null;
  readonly to: Date | null;
  readonly isRefetching: boolean;
}): T[] {
  void params.isRefetching;
  return filterRowsToWindow(params.rows, params.from, params.to);
}
