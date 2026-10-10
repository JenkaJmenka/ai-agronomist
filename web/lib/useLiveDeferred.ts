import { useDeferredValue } from "react";

/**
 * Row list for chart/table rendering, deferred only while live streaming and
 * only once there is data to lag behind.
 *
 * Deferring exists so the chart/table can lag a frame behind the tiles while
 * live SSE ticks land every second, keeping nav and clicks responsive. It must
 * not cover the arrival of the first rows: `useDeferredValue` returns the
 * *previous* value on an update and re-renders the new one at low priority, so
 * an `[]` → populated transition keeps rendering `[]` while the populated
 * commit sits in background work — under CPU contention that can starve for
 * seconds (see `docs/handoff/web_deferred_value_mitigation.md`).
 *
 * `useDeferredValue` is called unconditionally (rules of hooks); only the
 * returned value is conditional.
 *
 * @param rows Freshest rows, stable across renders (memoize upstream).
 * @param live Whether live streaming is on (URL state `?live=true`).
 */
export function useLiveDeferred<T>(rows: T[], live: boolean): T[] {
  const deferredRows = useDeferredValue(rows);
  // Deferred still empty while rows are populated means the transition out of
  // empty is in flight — render the data now, smoothing resumes on the next
  // tick. Derived rather than latched in a ref so a discarded low-priority
  // render cannot leave the bypass stuck on.
  const isAwaitingFirstRows = deferredRows.length === 0 && rows.length > 0;
  if (!live || isAwaitingFirstRows) return rows;
  return deferredRows;
}
