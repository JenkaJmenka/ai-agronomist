import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ensureChartBaseline,
  readChartBaseline,
  rememberChartBaseline,
} from "@/components/charts/chartBaseline";

const SCOPE = "monitoring:tenant_1:gh_A";
const OTHER_SCOPE = "anomalies:tenant_1:gh_A";

describe("chartBaseline", () => {
  afterEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("stores and reads a baseline window for a given scope", () => {
    const from = new Date("2026-07-21T00:00:00.000Z");
    const to = new Date("2026-07-22T00:00:00.000Z");
    rememberChartBaseline(from, to, SCOPE);
    const baseline = readChartBaseline(SCOPE);
    expect(baseline?.from.toISOString()).toBe(from.toISOString());
    expect(baseline?.to.toISOString()).toBe(to.toISOString());
  });

  it("does not cross scopes — another scope returns null", () => {
    const from = new Date("2026-07-21T00:00:00.000Z");
    const to = new Date("2026-07-22T00:00:00.000Z");
    rememberChartBaseline(from, to, SCOPE);
    expect(readChartBaseline(OTHER_SCOPE)).toBeNull();
  });

  it("different scopes store independently", () => {
    const fromA = new Date("2026-07-21T00:00:00.000Z");
    const toA = new Date("2026-07-22T00:00:00.000Z");
    const fromB = new Date("2026-07-01T00:00:00.000Z");
    const toB = new Date("2026-07-08T00:00:00.000Z");
    rememberChartBaseline(fromA, toA, SCOPE);
    rememberChartBaseline(fromB, toB, OTHER_SCOPE);
    expect(readChartBaseline(SCOPE)?.from.toISOString()).toBe(fromA.toISOString());
    expect(readChartBaseline(OTHER_SCOPE)?.from.toISOString()).toBe(fromB.toISOString());
  });

  it("ensureChartBaseline only seeds when empty for that scope", () => {
    const first = {
      from: new Date("2026-07-21T00:00:00.000Z"),
      to: new Date("2026-07-22T00:00:00.000Z"),
    };
    const second = {
      from: new Date("2026-07-21T12:00:00.000Z"),
      to: new Date("2026-07-21T13:00:00.000Z"),
    };
    ensureChartBaseline(first.from, first.to, SCOPE);
    ensureChartBaseline(second.from, second.to, SCOPE);
    const baseline = readChartBaseline(SCOPE);
    expect(baseline?.from.toISOString()).toBe(first.from.toISOString());
  });

  it("rememberChartBaseline overwrites within the same scope", () => {
    rememberChartBaseline(
      new Date("2026-07-21T00:00:00.000Z"),
      new Date("2026-07-22T00:00:00.000Z"),
      SCOPE,
    );
    rememberChartBaseline(
      new Date("2026-07-01T00:00:00.000Z"),
      new Date("2026-07-08T00:00:00.000Z"),
      SCOPE,
    );
    expect(readChartBaseline(SCOPE)?.from.toISOString()).toBe("2026-07-01T00:00:00.000Z");
  });

  it("ignores an inverted window (to <= from)", () => {
    const from = new Date("2026-07-22T00:00:00.000Z");
    const to = new Date("2026-07-21T00:00:00.000Z"); // earlier than from
    rememberChartBaseline(from, to, SCOPE);
    expect(readChartBaseline(SCOPE)).toBeNull();
  });
});
