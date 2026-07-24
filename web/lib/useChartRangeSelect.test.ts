/** @vitest-environment jsdom */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RANGE_WRITE_DEBOUNCE_MS, useChartRangeSelect } from "@/lib/useChartRangeSelect";

// ---------------------------------------------------------------------------
// Stub out nuqs hooks so we don't need a provider in unit tests.
// ---------------------------------------------------------------------------
const mockSetRange = vi.fn().mockResolvedValue(new URLSearchParams());

vi.mock("@/lib/useDateRange", () => ({
  useDateRange: () => [{ from: null, to: null }, mockSetRange],
}));

describe("useChartRangeSelect", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockSetRange.mockClear();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("dims immediately on onChartRangeSelect even when readings are idle", () => {
    const { result } = renderHook(() => useChartRangeSelect({ isReadingsBusy: false }));
    expect(result.current.isDimmed).toBe(false);

    act(() => {
      result.current.onChartRangeSelect(new Date("2026-07-21"), new Date("2026-07-22"));
    });
    expect(result.current.isDimmed).toBe(true);
  });

  it(`debounces the URL write by ${RANGE_WRITE_DEBOUNCE_MS} ms`, () => {
    const { result } = renderHook(() => useChartRangeSelect({ isReadingsBusy: false }));
    const from = new Date("2026-07-21T00:00:00.000Z");
    const to = new Date("2026-07-22T00:00:00.000Z");

    act(() => {
      result.current.onChartRangeSelect(from, to);
    });
    expect(mockSetRange).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(RANGE_WRITE_DEBOUNCE_MS);
    });
    expect(mockSetRange).toHaveBeenCalledOnce();
    expect(mockSetRange).toHaveBeenCalledWith({ from, to });
  });

  it("debounces rapid successive calls — only the last fires", () => {
    const { result } = renderHook(() => useChartRangeSelect({ isReadingsBusy: false }));
    const from1 = new Date("2026-07-20T00:00:00.000Z");
    const to1 = new Date("2026-07-21T00:00:00.000Z");
    const from2 = new Date("2026-07-21T00:00:00.000Z");
    const to2 = new Date("2026-07-22T00:00:00.000Z");

    act(() => {
      result.current.onChartRangeSelect(from1, to1);
      vi.advanceTimersByTime(RANGE_WRITE_DEBOUNCE_MS - 50);
      result.current.onChartRangeSelect(from2, to2);
    });
    act(() => {
      vi.advanceTimersByTime(RANGE_WRITE_DEBOUNCE_MS);
    });
    expect(mockSetRange).toHaveBeenCalledOnce();
    expect(mockSetRange).toHaveBeenCalledWith({ from: from2, to: to2 });
  });
});
