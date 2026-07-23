"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Minimum dim time so cache hits still paint one visible loading cue. */
export const WINDOW_BUSY_CUE_MIN_MS = 250;

/**
 * Hard undim — chart range select must not stay greyed out while slow G4
 * enrichment (or a hung readings request) keeps fetching.
 */
export const WINDOW_BUSY_CUE_MAX_MS = 8_000;

/**
 * Dim cue for chart/period window changes.
 *
 * Chart range select updates the URL and client-filters rows immediately, so
 * React Query may never set ``isRefetching`` (cache hit) — call
 * ``beginWindowChange`` when writing the range so the dim still appears, then
 * clear once **readings** are idle (min cue time). Enrichment is intentionally
 * excluded: ``/anomalies/enriched`` often takes tens of seconds and already
 * has its own table loading state.
 */
export function useWindowBusyCue(options: { readonly isReadingsBusy: boolean }): {
  readonly isDimmed: boolean;
  readonly beginWindowChange: () => void;
} {
  const { isReadingsBusy } = options;
  const [armed, setArmed] = useState(false);
  const [readingsBusyHeldTooLong, setReadingsBusyHeldTooLong] = useState(false);
  const armedAtRef = useRef<number | null>(null);

  const beginWindowChange = useCallback(() => {
    armedAtRef.current = performance.now();
    setArmed(true);
    setReadingsBusyHeldTooLong(false);
  }, []);

  // Clear the armed cue once readings settle (honor min flash), or at max.
  useEffect(() => {
    if (!armed) return;

    if (isReadingsBusy) {
      const started = armedAtRef.current ?? performance.now();
      const remaining = Math.max(0, WINDOW_BUSY_CUE_MAX_MS - (performance.now() - started));
      const id = window.setTimeout(() => {
        armedAtRef.current = null;
        setArmed(false);
      }, remaining);
      return () => window.clearTimeout(id);
    }

    const started = armedAtRef.current ?? performance.now();
    const wait = Math.max(0, WINDOW_BUSY_CUE_MIN_MS - (performance.now() - started));
    const id = window.setTimeout(() => {
      armedAtRef.current = null;
      setArmed(false);
    }, wait);
    return () => window.clearTimeout(id);
  }, [armed, isReadingsBusy]);

  // Period-preset refetches dim via isReadingsBusy alone — cap that too so a
  // hung / very slow window fetch cannot leave the page grey forever.
  useEffect(() => {
    if (!isReadingsBusy) {
      setReadingsBusyHeldTooLong(false);
      return;
    }
    const id = window.setTimeout(() => {
      setReadingsBusyHeldTooLong(true);
    }, WINDOW_BUSY_CUE_MAX_MS);
    return () => window.clearTimeout(id);
  }, [isReadingsBusy]);

  const readingsDim = isReadingsBusy && !readingsBusyHeldTooLong;

  return {
    isDimmed: armed || readingsDim,
    beginWindowChange,
  };
}
