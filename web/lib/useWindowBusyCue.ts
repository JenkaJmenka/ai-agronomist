"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Minimum dim time so cache hits still paint one visible loading cue. */
export const WINDOW_BUSY_CUE_MIN_MS = 250;

/**
 * Dim cue for chart/period window changes.
 *
 * Chart range select updates the URL and client-filters rows immediately, so
 * React Query may never set ``isRefetching`` (cache hit) — call
 * ``beginWindowChange`` when writing the range so the dim still appears, then
 * clear once readings (+ optional enrichment) are idle and the min time elapsed.
 */
export function useWindowBusyCue(options: {
  readonly isReadingsBusy: boolean;
  readonly isEnrichmentBusy?: boolean;
}): {
  readonly isDimmed: boolean;
  readonly beginWindowChange: () => void;
} {
  const { isReadingsBusy, isEnrichmentBusy = false } = options;
  const [armed, setArmed] = useState(false);
  const armedAtRef = useRef<number | null>(null);

  const beginWindowChange = useCallback(() => {
    armedAtRef.current = performance.now();
    setArmed(true);
  }, []);

  const networkBusy = isReadingsBusy || isEnrichmentBusy;

  useEffect(() => {
    if (!armed) return;
    if (networkBusy) return;

    const started = armedAtRef.current ?? performance.now();
    const wait = Math.max(0, WINDOW_BUSY_CUE_MIN_MS - (performance.now() - started));
    const id = window.setTimeout(() => {
      armedAtRef.current = null;
      setArmed(false);
    }, wait);
    return () => window.clearTimeout(id);
  }, [armed, networkBusy]);

  return {
    isDimmed: armed || networkBusy,
    beginWindowChange,
  };
}
