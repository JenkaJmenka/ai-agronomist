/** Shared period / refresh presets for monitoring window controls. */

export interface PeriodPreset {
  readonly id: string;
  readonly label: string;
  /** Lookback duration in hours (fractional for sub-hour windows). */
  readonly hours: number;
}

export const PERIOD_PRESETS: readonly PeriodPreset[] = [
  { id: "15m", label: "Last 15 min", hours: 0.25 },
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
 * Historical uploads often end days/months before wall-clock "now". Anchoring
 * to the newest stored timestamp (like `/api/readings/recent`) keeps
 * "Last hour / 24h / 7d" filled. While Go Live is on, prefer wall-clock so the
 * rolling window includes incoming ticks; if stored data is ahead of now, use it.
 */
export function periodWindowEnd(options: {
  readonly maxTimestamp: Date | null;
  readonly live: boolean;
  readonly now?: Date;
}): Date {
  const now = options.now ?? new Date();
  const maxTs = options.maxTimestamp;
  if (options.live) {
    if (maxTs !== null && maxTs.getTime() > now.getTime()) return maxTs;
    return now;
  }
  return maxTs ?? now;
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
