/**
 * Shared time-formatting helpers for ISO-8601 timestamps.
 *
 * Extracted from features/monitoring and features/jobs to eliminate duplication
 * (previously `formatTime` in LiveAnomalyStrip.tsx and `formatTimestamp` in
 * JobCard.tsx were near-identical functions — tech-lead review finding #17).
 */

/** Format ISO-8601 timestamp as HH:MM. Returns a truncated slice on parse failure. */
export function formatTime(iso: string): string {
  try {
    return new Date(iso).toISOString().slice(11, 16);
  } catch {
    return iso.slice(0, 16);
  }
}

/**
 * Format ISO-8601 timestamp as "YYYY-MM-DD HH:MM:SS".
 * Returns "—" for null/undefined/empty input.
 */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 19);
}
