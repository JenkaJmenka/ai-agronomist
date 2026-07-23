import { describe, expect, it } from "vitest";

import {
  MULTI_ROW_EDGE_PAD_MS,
  SINGLE_ROW_PAD_MS,
  windowFromSelectedTimestamps,
} from "@/lib/selectionWindow";

describe("windowFromSelectedTimestamps", () => {
  it("returns null for empty or invalid timestamps", () => {
    expect(windowFromSelectedTimestamps([])).toBeNull();
    expect(windowFromSelectedTimestamps(["not-a-date"])).toBeNull();
  });

  it("centers ±15 min on a single row without clamping to wall-clock now", () => {
    // Demo / upload data can sit months ahead of the browser clock — clamping
    // would jump the chart away from the selected row (live Monitoring bug).
    const win = windowFromSelectedTimestamps(["2026-12-31T23:55:00.000Z"]);
    expect(win?.from.toISOString()).toBe("2026-12-31T23:40:00.000Z");
    expect(win?.to.toISOString()).toBe("2027-01-01T00:10:00.000Z");
    if (!win) throw new Error("expected window");
    expect(win.to.getTime() - win.from.getTime()).toBe(SINGLE_ROW_PAD_MS * 2);
  });

  it("spans earliest→latest with edge pad for multiple rows", () => {
    const win = windowFromSelectedTimestamps([
      "2026-07-23T14:00:00.000Z",
      "2026-07-23T12:00:00.000Z",
      "2026-07-23T13:00:00.000Z",
    ]);
    expect(win?.from.toISOString()).toBe(
      new Date(Date.parse("2026-07-23T12:00:00.000Z") - MULTI_ROW_EDGE_PAD_MS).toISOString(),
    );
    expect(win?.to.toISOString()).toBe(
      new Date(Date.parse("2026-07-23T14:00:00.000Z") + MULTI_ROW_EDGE_PAD_MS).toISOString(),
    );
  });

  it("pads identical multi-select timestamps with the multi-row edge pad", () => {
    const win = windowFromSelectedTimestamps([
      "2026-07-23T12:00:00.000Z",
      "2026-07-23T12:00:00.000Z",
    ]);
    expect(win?.from.toISOString()).toBe(
      new Date(Date.parse("2026-07-23T12:00:00.000Z") - MULTI_ROW_EDGE_PAD_MS).toISOString(),
    );
    expect(win?.to.toISOString()).toBe(
      new Date(Date.parse("2026-07-23T12:00:00.000Z") + MULTI_ROW_EDGE_PAD_MS).toISOString(),
    );
  });
});
