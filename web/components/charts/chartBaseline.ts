/**
 * Session baseline for chart Reset — the last Period / custom window the
 * operator chose. Drag-select and scroll-zoom update the URL without touching
 * this, so Reset can restore the pre-zoom window.
 */

import type { TimeWindow } from "@/components/charts/timeWindow";

const STORAGE_KEY = "ai-agronomist.chart-baseline";

export function rememberChartBaseline(from: Date, to: Date): void {
  if (!(to.getTime() > from.getTime())) return;
  try {
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ from: from.toISOString(), to: to.toISOString() }),
    );
  } catch {
    // sessionStorage may be unavailable (private mode / SSR).
  }
}

export function readChartBaseline(): TimeWindow | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const record = parsed as { from?: unknown; to?: unknown };
    if (typeof record.from !== "string" || typeof record.to !== "string") return null;
    const from = new Date(record.from);
    const to = new Date(record.to);
    if (!Number.isFinite(from.getTime()) || !Number.isFinite(to.getTime())) return null;
    if (!(to.getTime() > from.getTime())) return null;
    return { from, to };
  } catch {
    return null;
  }
}

/** Seed baseline once so an existing URL window can be restored after zoom. */
export function ensureChartBaseline(from: Date, to: Date): void {
  if (readChartBaseline() === null) rememberChartBaseline(from, to);
}
