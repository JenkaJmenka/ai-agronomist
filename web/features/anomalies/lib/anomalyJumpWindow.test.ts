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

  describe("dataset timestamped ahead of the clock", () => {
    // Reported 2026-09-14: an uploaded export carried readings through
    // December while the clock said September. Clamping to `now` slid the
    // window three months off the anomaly, onto unscored September rows —
    // a plausible-looking chart with nothing highlighted.
    const clock = new Date("2026-09-14T10:46:32.059Z");
    const anomaly = "2026-12-25T10:25:00.000Z";

    it("keeps the anomaly inside the window when the dataset extends past now", () => {
      const win = anomalyJumpWindow(anomaly, {
        now: clock,
        datasetEnd: "2026-12-31T23:55:00.000Z",
      });
      expect(win).not.toBeNull();
      if (win === null) return;
      expect(win.from.toISOString()).toBe("2026-12-24T22:25:00.000Z");
      expect(win.to.toISOString()).toBe("2026-12-25T22:25:00.000Z");
      const center = Date.parse(anomaly);
      expect(win.from.getTime()).toBeLessThanOrEqual(center);
      expect(win.to.getTime()).toBeGreaterThanOrEqual(center);
    });

    it("clamps to the newest stored row, keeping the anomaly at the edge", () => {
      // Anomaly is the newest row: the forward pad has no data to show.
      const win = anomalyJumpWindow(anomaly, { now: clock, datasetEnd: anomaly });
      expect(win).not.toBeNull();
      if (win === null) return;
      expect(win.to.toISOString()).toBe(anomaly);
      expect(win.from.toISOString()).toBe("2026-12-24T10:25:00.000Z");
      expect(win.from.getTime()).toBeLessThanOrEqual(Date.parse(anomaly));
    });

    it("still clamps to now when bounds are unknown", () => {
      // Regression guard: without datasetEnd the old wall-clock behaviour
      // stands, so a genuinely-future timestamp cannot open an empty window.
      const win = anomalyJumpWindow(anomaly, { now: clock });
      expect(win?.to.toISOString()).toBe(clock.toISOString());
      expect(win?.from.toISOString()).toBe("2026-09-13T10:46:32.059Z");
    });
  });
});
