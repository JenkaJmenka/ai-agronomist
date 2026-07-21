import { describe, expect, it } from "vitest";

import {
  matchPeriodId,
  periodWindowEnd,
  windowForPeriod,
} from "@/lib/periodPresets";

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

  it("prefers wall-clock while live so incoming ticks stay in range", () => {
    expect(periodWindowEnd({ maxTimestamp: maxTs, live: true, now }).toISOString()).toBe(
      "2026-07-21T14:00:00.000Z",
    );
  });

  it("uses stored max when it is ahead of wall-clock while live", () => {
    const ahead = new Date("2026-07-21T15:00:00.000Z");
    expect(periodWindowEnd({ maxTimestamp: ahead, live: true, now }).toISOString()).toBe(
      "2026-07-21T15:00:00.000Z",
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
});

describe("matchPeriodId", () => {
  it("matches a 1h span to the Last hour preset", () => {
    const to = new Date("2026-06-03T11:00:00.000Z");
    const from = new Date(to.getTime() - 3_600_000);
    expect(matchPeriodId(from, to)).toBe("1h");
  });
});
