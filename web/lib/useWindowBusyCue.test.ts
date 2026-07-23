/** @vitest-environment jsdom */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useWindowBusyCue, WINDOW_BUSY_CUE_MIN_MS } from "./useWindowBusyCue";

describe("useWindowBusyCue", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("dims immediately on beginWindowChange even when network is idle", () => {
    const { result } = renderHook(() =>
      useWindowBusyCue({ isReadingsBusy: false, isEnrichmentBusy: false }),
    );
    expect(result.current.isDimmed).toBe(false);

    act(() => {
      result.current.beginWindowChange();
    });
    expect(result.current.isDimmed).toBe(true);
  });

  it("stays dim while enrichment is busy, then clears after min cue time", () => {
    const { result, rerender } = renderHook(
      ({ readings, enrichment }: { readings: boolean; enrichment: boolean }) =>
        useWindowBusyCue({ isReadingsBusy: readings, isEnrichmentBusy: enrichment }),
      { initialProps: { readings: false, enrichment: false } },
    );

    act(() => {
      result.current.beginWindowChange();
    });
    rerender({ readings: true, enrichment: true });
    expect(result.current.isDimmed).toBe(true);

    rerender({ readings: false, enrichment: true });
    expect(result.current.isDimmed).toBe(true);

    rerender({ readings: false, enrichment: false });
    expect(result.current.isDimmed).toBe(true);

    act(() => {
      vi.advanceTimersByTime(WINDOW_BUSY_CUE_MIN_MS);
    });
    expect(result.current.isDimmed).toBe(false);
  });

  it("dims from network busy alone without beginWindowChange", () => {
    const { result, rerender } = renderHook(
      ({ busy }: { busy: boolean }) => useWindowBusyCue({ isReadingsBusy: busy }),
      { initialProps: { busy: false } },
    );
    rerender({ busy: true });
    expect(result.current.isDimmed).toBe(true);
    rerender({ busy: false });
    expect(result.current.isDimmed).toBe(false);
  });
});
