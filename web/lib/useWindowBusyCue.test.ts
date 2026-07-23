/** @vitest-environment jsdom */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  useWindowBusyCue,
  WINDOW_BUSY_CUE_MAX_MS,
  WINDOW_BUSY_CUE_MIN_MS,
} from "./useWindowBusyCue";

describe("useWindowBusyCue", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("dims immediately on beginWindowChange even when network is idle", () => {
    const { result } = renderHook(() => useWindowBusyCue({ isReadingsBusy: false }));
    expect(result.current.isDimmed).toBe(false);

    act(() => {
      result.current.beginWindowChange();
    });
    expect(result.current.isDimmed).toBe(true);
  });

  it("clears after readings settle and min cue time (ignores enrichment)", () => {
    const { result, rerender } = renderHook(
      ({ readings }: { readings: boolean }) => useWindowBusyCue({ isReadingsBusy: readings }),
      { initialProps: { readings: false } },
    );

    act(() => {
      result.current.beginWindowChange();
    });
    rerender({ readings: true });
    expect(result.current.isDimmed).toBe(true);

    rerender({ readings: false });
    expect(result.current.isDimmed).toBe(true);

    act(() => {
      vi.advanceTimersByTime(WINDOW_BUSY_CUE_MIN_MS);
    });
    expect(result.current.isDimmed).toBe(false);
  });

  it("dims from readings busy alone without beginWindowChange", () => {
    const { result, rerender } = renderHook(
      ({ busy }: { busy: boolean }) => useWindowBusyCue({ isReadingsBusy: busy }),
      { initialProps: { busy: false } },
    );
    rerender({ busy: true });
    expect(result.current.isDimmed).toBe(true);
    rerender({ busy: false });
    expect(result.current.isDimmed).toBe(false);
  });

  it("force-clears an armed cue when readings stay busy past max", () => {
    const { result, rerender } = renderHook(
      ({ readings }: { readings: boolean }) => useWindowBusyCue({ isReadingsBusy: readings }),
      { initialProps: { readings: false } },
    );

    act(() => {
      result.current.beginWindowChange();
    });
    rerender({ readings: true });
    expect(result.current.isDimmed).toBe(true);

    act(() => {
      vi.advanceTimersByTime(WINDOW_BUSY_CUE_MAX_MS);
    });
    expect(result.current.isDimmed).toBe(false);
  });

  it("stops dimming a hung readings refetch after max", () => {
    const { result, rerender } = renderHook(
      ({ busy }: { busy: boolean }) => useWindowBusyCue({ isReadingsBusy: busy }),
      { initialProps: { busy: true } },
    );
    expect(result.current.isDimmed).toBe(true);

    act(() => {
      vi.advanceTimersByTime(WINDOW_BUSY_CUE_MAX_MS);
    });
    expect(result.current.isDimmed).toBe(false);

    // Still "busy" upstream, but cue must not stick.
    rerender({ busy: true });
    expect(result.current.isDimmed).toBe(false);
  });
});
