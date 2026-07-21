import { describe, expect, it } from "vitest";

import {
  filterRowsToWindow,
  matchPeriodId,
  PERIOD_PRESETS,
  periodWindowEnd,
  windowForPeriod,
} from "@/lib/periodPresets";

describe("PERIOD_PRESETS", () => {
  it("includes last 15 and 30 minute options", () => {
    expect(PERIOD_PRESETS.map((p) => p.id)).toContain("15m");
    expect(PERIOD_PRESETS.map((p) => p.id)).toContain("30m");
    expect(PERIOD_PRESETS.find((p) => p.id === "30m")?.hours).toBe(0.5);
  });
});

describe("periodWindowEnd", () => {
  const now = new Date("2026-07-21T14:00:00.000Z");
  const maxTs = new Date("2026-06-03T11:00:00.000Z");

  it("anchors to newest stored data when not live", () => {
    expect(periodWindowEnd({ maxTimestamp: maxTs, live: false, now }).toISOString()).toBe(
      "2026-06-03T11:00:00.000Z",
    );
  });

  it("falls back to wall-clock when no stored data and not live", () => {
    expect(periodWindowEnd({ maxTimestamp: null, live: false, now }).toISOString()).toBe(
      "2026-07-21T14:00:00.000Z",
    );
  });

  it("uses wall-clock while live when stored data is behind now", () => {
    expect(periodWindowEnd({ maxTimestamp: maxTs, live: true, now }).toISOString()).toBe(
      "2026-07-21T14:00:00.000Z",
    );
  });

  it("uses stored max when it is ahead of wall-clock (live or not)", () => {
    const ahead = new Date("2026-12-31T23:55:00.000Z");
    expect(periodWindowEnd({ maxTimestamp: ahead, live: true, now }).toISOString()).toBe(
      "2026-12-31T23:55:00.000Z",
    );
    expect(periodWindowEnd({ maxTimestamp: ahead, live: false, now }).toISOString()).toBe(
      "2026-12-31T23:55:00.000Z",
    );
  });
});

describe("windowForPeriod", () => {
  it("builds a lookback ending at the given anchor", () => {
    const end = new Date("2026-06-03T11:00:00.000Z");
    const { from, to } = windowForPeriod(24, end);
    expect(to.toISOString()).toBe("2026-06-03T11:00:00.000Z");
    expect(from.toISOString()).toBe("2026-06-02T11:00:00.000Z");
  });

  it("supports a 30-minute lookback", () => {
    const end = new Date("2026-12-31T23:55:00.000Z");
    const { from, to } = windowForPeriod(0.5, end);
    expect(to.toISOString()).toBe("2026-12-31T23:55:00.000Z");
    expect(from.toISOString()).toBe("2026-12-31T23:25:00.000Z");
  });
});

describe("matchPeriodId", () => {
  it("matches a 1h span to the Last hour preset", () => {
    const to = new Date("2026-06-03T11:00:00.000Z");
    const from = new Date(to.getTime() - 3_600_000);
    expect(matchPeriodId(from, to)).toBe("1h");
  });

  it("matches a 30-minute span", () => {
    const to = new Date("2026-12-31T23:55:00.000Z");
    const from = new Date(to.getTime() - 30 * 60_000);
    expect(matchPeriodId(from, to)).toBe("30m");
  });
});

describe("filterRowsToWindow", () => {
  const rows = [
    { timestamp: "2026-07-01T00:00:00.000Z" },
    { timestamp: "2026-12-31T23:45:00.000Z" },
    { timestamp: "2026-12-31T23:50:00.000Z" },
    { timestamp: "2027-01-01T00:10:00.000Z" },
  ];

  it("keeps only rows inside the period window", () => {
    const from = new Date("2026-12-31T23:40:00.000Z");
    const to = new Date("2026-12-31T23:55:00.000Z");
    expect(filterRowsToWindow(rows, from, to).map((r) => r.timestamp)).toEqual([
      "2026-12-31T23:45:00.000Z",
      "2026-12-31T23:50:00.000Z",
    ]);
  });
});
