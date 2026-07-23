import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ensureChartBaseline,
  readChartBaseline,
  rememberChartBaseline,
} from "@/components/charts/chartBaseline";

describe("chartBaseline", () => {
  afterEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("stores and reads a baseline window", () => {
    const from = new Date("2026-07-21T00:00:00.000Z");
    const to = new Date("2026-07-22T00:00:00.000Z");
    rememberChartBaseline(from, to);
    const baseline = readChartBaseline();
    expect(baseline?.from.toISOString()).toBe(from.toISOString());
    expect(baseline?.to.toISOString()).toBe(to.toISOString());
  });

  it("ensureChartBaseline only seeds when empty", () => {
    const first = {
      from: new Date("2026-07-21T00:00:00.000Z"),
      to: new Date("2026-07-22T00:00:00.000Z"),
    };
    const second = {
      from: new Date("2026-07-21T12:00:00.000Z"),
      to: new Date("2026-07-21T13:00:00.000Z"),
    };
    ensureChartBaseline(first.from, first.to);
    ensureChartBaseline(second.from, second.to);
    const baseline = readChartBaseline();
    expect(baseline?.from.toISOString()).toBe(first.from.toISOString());
  });

  it("rememberChartBaseline overwrites", () => {
    rememberChartBaseline(
      new Date("2026-07-21T00:00:00.000Z"),
      new Date("2026-07-22T00:00:00.000Z"),
    );
    rememberChartBaseline(
      new Date("2026-07-01T00:00:00.000Z"),
      new Date("2026-07-08T00:00:00.000Z"),
    );
    expect(readChartBaseline()?.from.toISOString()).toBe("2026-07-01T00:00:00.000Z");
  });
});
