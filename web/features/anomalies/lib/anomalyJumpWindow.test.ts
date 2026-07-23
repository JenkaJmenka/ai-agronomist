import { describe, expect, it } from "vitest";

import { anomalyJumpWindow } from "@/features/anomalies/lib/anomalyJumpWindow";

describe("anomalyJumpWindow", () => {
  const now = new Date("2026-07-23T14:18:00.000Z");

  it("centers ±12h when the forward pad is still in the past", () => {
    const win = anomalyJumpWindow("2026-07-20T12:00:00.000Z", { now });
    expect(win?.from.toISOString()).toBe("2026-07-20T00:00:00.000Z");
    expect(win?.to.toISOString()).toBe("2026-07-21T00:00:00.000Z");
  });

  it("clamps to now and keeps a 24h span when the anomaly is recent", () => {
    // Same scenario as the broken Monitoring screenshot: anomaly at 12:06,
    // wall-clock 14:18 → old ±12h put empty future on the right half.
    const win = anomalyJumpWindow("2026-07-23T12:06:14.000Z", { now });
    expect(win).not.toBeNull();
    if (win === null) return;
    expect(win.to.toISOString()).toBe(now.toISOString());
    expect(win.from.toISOString()).toBe("2026-07-22T14:18:00.000Z");
    const spanH = (win.to.getTime() - win.from.getTime()) / 3_600_000;
    expect(spanH).toBe(24);
  });

  it("returns null for an invalid timestamp", () => {
    expect(anomalyJumpWindow("not-a-date", { now })).toBeNull();
  });
});
