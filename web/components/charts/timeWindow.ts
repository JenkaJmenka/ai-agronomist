/** Pure helpers for URL-driven chart pan / zoom. */

export interface TimeWindow {
  readonly from: Date;
  readonly to: Date;
}

const MAX_SPAN_MS = 182 * 24 * 3_600_000; // 6 months — matches longest preset
const MIN_SPAN_MS = 60_000; // 1 minute — floor for scroll zoom-in

/** Shift a window by a fraction of its span (positive → later / newer). */
export function panWindow(window: TimeWindow, deltaFraction: number): TimeWindow {
  const span = window.to.getTime() - window.from.getTime();
  if (!(span > 0) || !Number.isFinite(deltaFraction)) return window;
  const shift = span * deltaFraction;
  return {
    from: new Date(window.from.getTime() + shift),
    to: new Date(window.to.getTime() + shift),
  };
}

/**
 * Scale a window about its midpoint.
 * ``factor > 1`` zooms out (wider); ``factor < 1`` zooms in (narrower).
 * Span is clamped to [1 minute, 6 months].
 */
export function zoomWindow(window: TimeWindow, factor: number): TimeWindow {
  const span = window.to.getTime() - window.from.getTime();
  if (!(span > 0) || !(factor > 0) || factor === 1) return window;
  let nextSpan = span * factor;
  if (factor > 1) nextSpan = Math.min(nextSpan, MAX_SPAN_MS);
  else nextSpan = Math.max(nextSpan, MIN_SPAN_MS);
  if (nextSpan === span) return window;
  const mid = (window.from.getTime() + window.to.getTime()) / 2;
  const half = nextSpan / 2;
  return {
    from: new Date(mid - half),
    to: new Date(mid + half),
  };
}

/** Expand a window about its midpoint (default 2×). Caps at 6 months. */
export function zoomOutWindow(window: TimeWindow, factor = 2): TimeWindow {
  if (!(factor > 1)) return window;
  return zoomWindow(window, factor);
}

/** Read the current x-scale window from a live uPlot instance (unix seconds). */
export function scaleWindowSec(
  min: number | null | undefined,
  max: number | null | undefined,
): TimeWindow | null {
  if (min == null || max == null || !(max > min)) return null;
  return {
    from: new Date(min * 1000),
    to: new Date(max * 1000),
  };
}
