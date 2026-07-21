import { describe, expect, it } from "vitest";

import { pickDashboardSearchParams, withDashboardQuery } from "@/lib/dashboardQuery";

describe("dashboardQuery", () => {
  it("keeps shared window/live/threshold params and drops unrelated ones", () => {
    const search = new URLSearchParams({
      tenant: "tenant_1",
      gh: "gh_A",
      from: "2026-06-01T00:00:00.000Z",
      to: "2026-06-02T00:00:00.000Z",
      live: "true",
      refresh: "30",
      threshold: "0.2",
      channel: "temperature",
      view: "table",
      junk: "1",
    });
    const picked = pickDashboardSearchParams(search);
    expect(Object.fromEntries(picked)).toEqual({
      tenant: "tenant_1",
      gh: "gh_A",
      from: "2026-06-01T00:00:00.000Z",
      to: "2026-06-02T00:00:00.000Z",
      live: "true",
      refresh: "30",
      threshold: "0.2",
      channel: "temperature",
    });
    expect(picked.has("view")).toBe(false);
    expect(picked.has("junk")).toBe(false);
  });

  it("builds href with query suffix when shared params exist", () => {
    const search = new URLSearchParams({ tenant: "t1", live: "true" });
    expect(withDashboardQuery("/climate", search)).toBe("/climate?tenant=t1&live=true");
  });

  it("returns bare href when nothing shared is set", () => {
    expect(withDashboardQuery("/monitoring", new URLSearchParams())).toBe("/monitoring");
  });
});
