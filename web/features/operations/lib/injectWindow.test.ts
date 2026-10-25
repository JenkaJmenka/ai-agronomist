import { describe, expect, it } from "vitest";

import {
  INJECT_WINDOW_SPAN_HOURS,
  resolveInjectWindow,
} from "@/features/operations/lib/injectWindow";
import { DEFAULT_PERIOD_ID, periodHours } from "@/lib/periodPresets";

/**
 * Pinned to the literal 24h the production bounds justified, not derived
 * from `INJECT_WINDOW_SPAN_HOURS` — deriving it from the constant under test
 * would make every assertion below a tautology (`x === x`) that cannot catch
 * the span being wrong.
 */
const EXPECTED_SPAN_HOURS = 24;
const SPAN_MS = EXPECTED_SPAN_HOURS * 3_600_000;

function assertSpanHolds(window: { windowStart: string; windowEnd: string }) {
  const spanMs = new Date(window.windowEnd).getTime() - new Date(window.windowStart).getTime();
  expect(spanMs).toBe(SPAN_MS);
}

describe("INJECT_WINDOW_SPAN_HOURS", () => {
  // Both sides pinned to the literal 24 (not to each other) so changing
  // DEFAULT_PERIOD_ID alone breaks this test — that is intentional: a
  // failure here means "reconsider injection's span to match", not "update
  // this number".
  it("is 24h, matching the default view period", () => {
    expect(INJECT_WINDOW_SPAN_HOURS).toBe(24);
    expect(periodHours(DEFAULT_PERIOD_ID)).toBe(24);
  });
});

describe("resolveInjectWindow", () => {
  const now = new Date("2026-10-25T12:00:00.000Z");

  it("anchors to the newest stored timestamp when it is ahead of now", () => {
    const maxTimestamp = new Date("2026-12-31T23:00:00.000Z");
    const window = resolveInjectWindow({ maxTimestamp, now });
    expect(window.windowEnd).toBe(maxTimestamp.toISOString());
    assertSpanHolds(window);
  });

  it("anchors to the newest stored timestamp when it is behind now", () => {
    const maxTimestamp = new Date("2026-06-01T00:00:00.000Z");
    const window = resolveInjectWindow({ maxTimestamp, now });
    expect(window.windowEnd).toBe(maxTimestamp.toISOString());
    assertSpanHolds(window);
  });

  it("falls back to wall-clock now when there is no stored timestamp", () => {
    const window = resolveInjectWindow({ maxTimestamp: null, now });
    expect(window.windowEnd).toBe(now.toISOString());
    assertSpanHolds(window);
  });
});
