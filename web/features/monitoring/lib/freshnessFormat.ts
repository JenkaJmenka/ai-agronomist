/**
 * Human-readable phrasing for `freshness.age_seconds` / `threshold_seconds`
 * (see `src/api/services/live_freshness.py`). An operator acts faster on
 * "last reading 27 days ago" than on a raw `age_seconds: 2332800`.
 */

const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE;
const SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR;

function pluralize(value: number, unit: string): string {
  return `${value} ${unit}${value === 1 ? "" : "s"}`;
}

/** Largest whole unit that fits `totalSeconds` (always non-negative input). */
export function formatDurationSeconds(totalSeconds: number): string {
  const seconds = Math.floor(totalSeconds);
  if (seconds < SECONDS_PER_MINUTE) return pluralize(seconds, "second");
  if (seconds < SECONDS_PER_HOUR) {
    return pluralize(Math.floor(seconds / SECONDS_PER_MINUTE), "minute");
  }
  if (seconds < SECONDS_PER_DAY) {
    return pluralize(Math.floor(seconds / SECONDS_PER_HOUR), "hour");
  }
  return pluralize(Math.floor(seconds / SECONDS_PER_DAY), "day");
}

/**
 * Describe a reading's age for an operator.
 *
 * `ageSeconds === null` means the scope has never written a row at all —
 * the exact production state this feature exists to surface, so it gets
 * its own message rather than reading as "no data yet".
 *
 * `ageSeconds < 0` means the newest reading is timestamped in the future
 * (readings in this repo are routinely future-dated by design — see the
 * live_freshness module docstring). This is never rendered as an error.
 */
export function describeReadingAge(ageSeconds: number | null): string {
  if (ageSeconds === null) return "No reading has ever arrived for this scope";
  if (ageSeconds < 0) {
    return `Newest reading is ${formatDurationSeconds(-ageSeconds)} ahead of now (future-dated)`;
  }
  return `Last reading ${formatDurationSeconds(ageSeconds)} ago`;
}

/** Describe the staleness threshold a scope is being judged against. */
export function describeStaleThreshold(thresholdSeconds: number): string {
  return `stale after ${formatDurationSeconds(thresholdSeconds)} with no reading`;
}
