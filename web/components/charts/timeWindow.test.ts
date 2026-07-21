import { describe, expect, it } from "vitest";

import { panWindow, scaleWindowSec, zoomOutWindow } from "@/components/charts/timeWindow";

describe("timeWindow", () => {
  const base = {
    from: new Date("2026-07-21T11:00:00.000Z"),
    to: new Date("2026-07-21T12:00:00.000Z"),
  };

  it("pans by a fraction of the span", () => {
    const next = panWindow(base, 0.25);
    expect(next.from.toISOString()).toBe("2026-07-21T11:15:00.000Z");
    expect(next.to.toISOString()).toBe("2026-07-21T12:15:00.000Z");
  });

  it("zooms out 2× about the midpoint", () => {
    const next = zoomOutWindow(base, 2);
    expect(next.from.toISOString()).toBe("2026-07-21T10:30:00.000Z");
    expect(next.to.toISOString()).toBe("2026-07-21T12:30:00.000Z");
  });

  it("caps zoom-out at six months", () => {
    const wide = {
      from: new Date("2026-01-01T00:00:00.000Z"),
      to: new Date("2026-06-01T00:00:00.000Z"),
    };
    const next = zoomOutWindow(wide, 4);
    const spanMs = next.to.getTime() - next.from.getTime();
    expect(spanMs).toBeLessThanOrEqual(182 * 24 * 3_600_000);
  });

  it("reads a scale window from unix seconds", () => {
    const win = scaleWindowSec(1_000, 2_000);
    expect(win?.from.toISOString()).toBe("1970-01-01T00:16:40.000Z");
    expect(win?.to.toISOString()).toBe("1970-01-01T00:33:20.000Z");
    expect(scaleWindowSec(5, 5)).toBeNull();
  });
});
