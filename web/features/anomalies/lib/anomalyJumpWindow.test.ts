import { describe, expect, it } from "vitest";

import {
  anomalyJumpWindow,
  episodeJumpWindow,
  monitoringJumpWindow,
} from "@/features/anomalies/lib/anomalyJumpWindow";

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

describe("episodeJumpWindow", () => {
  const now = new Date("2026-07-23T14:18:00.000Z");

  it("floors a short episode to a 2h window so it is not a sliver of the chart", () => {
    const win = episodeJumpWindow(
      { start_ts: "2026-07-20T10:00:00.000Z", end_ts: "2026-07-20T10:10:00.000Z" },
      { now },
    );
    expect(win).not.toBeNull();
    if (win === null) return;
    expect(win.from.toISOString()).toBe("2026-07-20T09:05:00.000Z");
    expect(win.to.toISOString()).toBe("2026-07-20T11:05:00.000Z");
    const spanH = (win.to.getTime() - win.from.getTime()) / 3_600_000;
    expect(spanH).toBe(2);
  });

  it("pads a mid-length episode proportionally to its own duration", () => {
    const win = episodeJumpWindow(
      { start_ts: "2026-07-20T08:00:00.000Z", end_ts: "2026-07-20T12:00:00.000Z" },
      { now },
    );
    expect(win).not.toBeNull();
    if (win === null) return;
    expect(win.from.toISOString()).toBe("2026-07-20T06:00:00.000Z");
    expect(win.to.toISOString()).toBe("2026-07-20T14:00:00.000Z");
  });

  it("shows a long episode's exact span with no extra pad past the ceiling", () => {
    const win = episodeJumpWindow(
      { start_ts: "2026-07-18T00:00:00.000Z", end_ts: "2026-07-20T12:00:00.000Z" },
      { now },
    );
    expect(win).not.toBeNull();
    if (win === null) return;
    expect(win.from.toISOString()).toBe("2026-07-18T00:00:00.000Z");
    expect(win.to.toISOString()).toBe("2026-07-20T12:00:00.000Z");
  });

  it("clamps to the horizon and shifts from earlier to keep the padded span", () => {
    const win = episodeJumpWindow(
      { start_ts: "2026-07-23T11:30:00.000Z", end_ts: "2026-07-23T13:30:00.000Z" },
      { now },
    );
    expect(win).not.toBeNull();
    if (win === null) return;
    expect(win.to.toISOString()).toBe(now.toISOString());
    expect(win.from.toISOString()).toBe("2026-07-23T10:18:00.000Z");
  });

  it("returns null when the episode span is invalid (end before start)", () => {
    const win = episodeJumpWindow(
      { start_ts: "2026-07-20T12:00:00.000Z", end_ts: "2026-07-20T10:00:00.000Z" },
      { now },
    );
    expect(win).toBeNull();
  });

  it("returns null for an unparseable episode timestamp", () => {
    const win = episodeJumpWindow(
      { start_ts: "not-a-date", end_ts: "2026-07-20T10:00:00.000Z" },
      { now },
    );
    expect(win).toBeNull();
  });

  describe("dataset timestamped ahead of the clock", () => {
    // Same future-dated-export scenario as anomalyJumpWindow, applied to an
    // episode span instead of a single sample.
    const clock = new Date("2026-09-14T10:46:32.059Z");

    it("keeps the episode inside the window when the dataset extends past now", () => {
      const win = episodeJumpWindow(
        { start_ts: "2026-12-25T09:00:00.000Z", end_ts: "2026-12-25T11:00:00.000Z" },
        { now: clock, datasetEnd: "2026-12-31T23:55:00.000Z" },
      );
      expect(win).not.toBeNull();
      if (win === null) return;
      expect(win.from.toISOString()).toBe("2026-12-25T08:00:00.000Z");
      expect(win.to.toISOString()).toBe("2026-12-25T12:00:00.000Z");
    });

    it("still clamps to now when bounds are unknown", () => {
      const win = episodeJumpWindow(
        { start_ts: "2026-12-25T09:00:00.000Z", end_ts: "2026-12-25T11:00:00.000Z" },
        { now: clock },
      );
      expect(win).not.toBeNull();
      if (win === null) return;
      expect(win.to.toISOString()).toBe(clock.toISOString());
      expect(win.from.toISOString()).toBe("2026-09-14T06:46:32.059Z");
    });
  });
});

describe("monitoringJumpWindow", () => {
  const now = new Date("2026-07-23T14:18:00.000Z");
  const episode = { start_ts: "2026-07-20T08:00:00.000Z", end_ts: "2026-07-20T12:00:00.000Z" };

  it("frames the episode span when one is given, ignoring the clicked sample", () => {
    const win = monitoringJumpWindow({ timestamp: "2026-07-20T09:00:00.000Z", episode }, { now });
    expect(win).toEqual(episodeJumpWindow(episode, { now }));
  });

  it("falls back to the sample-centered window when no episode context is given", () => {
    const timestamp = "2026-07-20T12:00:00.000Z";
    const win = monitoringJumpWindow({ timestamp, episode: null }, { now });
    expect(win).toEqual(anomalyJumpWindow(timestamp, { now }));
  });

  it("falls back to the sample-centered window when the episode span is invalid", () => {
    const timestamp = "2026-07-20T12:00:00.000Z";
    const invalidEpisode = {
      start_ts: "2026-07-20T12:00:00.000Z",
      end_ts: "2026-07-20T10:00:00.000Z",
    };
    const win = monitoringJumpWindow({ timestamp, episode: invalidEpisode }, { now });
    expect(win).toEqual(anomalyJumpWindow(timestamp, { now }));
  });
});
