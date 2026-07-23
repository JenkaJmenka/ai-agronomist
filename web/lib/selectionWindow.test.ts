import { describe, expect, it } from "vitest";

import {
  MULTI_ROW_EDGE_PAD_MS,
  SINGLE_ROW_PAD_MS,
  windowFromSelectedTimestamps,
} from "@/lib/selectionWindow";

describe("windowFromSelectedTimestamps", () => {
  const now = new Date("2026-07-23T18:00:00.000Z");

  it("returns null for empty or invalid timestamps", () => {
    expect(windowFromSelectedTimestamps([])).toBeNull();
    expect(windowFromSelectedTimestamps(["not-a-date"])).toBeNull();
  });

  it("centers ±15 min on a single row when the forward pad is still in the past", () => {
    const win = windowFromSelectedTimestamps(["2026-07-23T12:00:00.000Z"], { now });
    expect(win?.from.toISOString()).toBe("2026-07-23T11:45:00.000Z");
    expect(win?.to.toISOString()).toBe("2026-07-23T12:15:00.000Z");
  });

  it("clamps a single-row window to now and keeps the pad span", () => {
    const win = windowFromSelectedTimestamps(["2026-07-23T17:55:00.000Z"], { now });
    expect(win).not.toBeNull();
    if (win === null) return;
    expect(win.to.toISOString()).toBe(now.toISOString());
    expect(win.from.getTime()).toBe(now.getTime() - SINGLE_ROW_PAD_MS * 2);
  });

  it("spans earliest→latest with edge pad for multiple rows", () => {
    const win = windowFromSelectedTimestamps(
      ["2026-07-23T14:00:00.000Z", "2026-07-23T12:00:00.000Z", "2026-07-23T13:00:00.000Z"],
      { now },
    );
    expect(win?.from.toISOString()).toBe(
      new Date(Date.parse("2026-07-23T12:00:00.000Z") - MULTI_ROW_EDGE_PAD_MS).toISOString(),
    );
    expect(win?.to.toISOString()).toBe(
      new Date(Date.parse("2026-07-23T14:00:00.000Z") + MULTI_ROW_EDGE_PAD_MS).toISOString(),
    );
  });
});
