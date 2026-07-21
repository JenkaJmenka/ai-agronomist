import { describe, expect, it } from "vitest";

import { TIME_AXIS_VALUES, TIME_X_AXIS } from "@/components/charts/timeAxis";

describe("timeAxis", () => {
  it("uses wider tick spacing than uPlot's dense default", () => {
    expect(TIME_X_AXIS.space).toBeGreaterThanOrEqual(80);
  });

  it("formats second-level ticks as clock time, not bare :ss", () => {
    const secondStamp = TIME_AXIS_VALUES.find((row: (string | number | null)[]) => row[0] === 1);
    expect(secondStamp?.[1]).toBe("{h}:{mm}{aa}");
  });

  it("keeps day rollover as a short second line without a glued date+time", () => {
    const minuteStamp = TIME_AXIS_VALUES.find((row: (string | number | null)[]) => row[0] === 60);
    expect(minuteStamp?.[2]).toBe("\n{MMM} {D}");
    expect(minuteStamp?.[4]).toBe("\n{MMM} {D}");
    expect(String(minuteStamp?.[2])).not.toMatch(/\{h\}/);
  });
});
