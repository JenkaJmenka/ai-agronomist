"use client";

import { useCallback, useEffect, useMemo } from "react";

import { debounce } from "@/lib/debounce";
import { useDateRange } from "@/lib/useDateRange";
import { useWindowBusyCue } from "@/lib/useWindowBusyCue";

/** Milliseconds the URL range write waits after the last chart drag/zoom event. */
export const RANGE_WRITE_DEBOUNCE_MS = 200;

interface UseChartRangeSelectOptions {
  readonly isReadingsBusy: boolean;
}

interface UseChartRangeSelectResult {
  readonly isDimmed: boolean;
  readonly onChartRangeSelect: (from: Date, to: Date) => void;
}

/**
 * Composes the window-busy dim cue with a debounced URL range write.
 * Shared by MonitoringView and AnomaliesView.
 *
 * `beginWindowChange` fires synchronously so the dim appears immediately;
 * the URL write is debounced at {@link RANGE_WRITE_DEBOUNCE_MS} so rapid
 * drag/scroll gestures do not flood React Query with refetch requests.
 * The debounce timer is cancelled on unmount.
 */
export function useChartRangeSelect({
  isReadingsBusy,
}: UseChartRangeSelectOptions): UseChartRangeSelectResult {
  const [, setRange] = useDateRange();
  const { isDimmed, beginWindowChange } = useWindowBusyCue({ isReadingsBusy });

  const debouncedSetRange = useMemo(
    () =>
      debounce((nextFrom: Date, nextTo: Date) => {
        void setRange({ from: nextFrom, to: nextTo });
      }, RANGE_WRITE_DEBOUNCE_MS),
    [setRange],
  );
  useEffect(() => () => debouncedSetRange.cancel(), [debouncedSetRange]);

  const onChartRangeSelect = useCallback(
    (nextFrom: Date, nextTo: Date) => {
      beginWindowChange();
      debouncedSetRange(nextFrom, nextTo);
    },
    [beginWindowChange, debouncedSetRange],
  );

  return { isDimmed, onChartRangeSelect };
}
