import { describe, expect, it } from "vitest";

import { collapseSetpointExcursions } from "@/features/anomalies/lib/setpointExcursions";
import type { SensorRow } from "@/features/monitoring/api/readings";

// ─── Helpers ────────────────────────────────────────────────────────────────

function ts(i: number): string {
  return new Date(1_000_000 + i * 300_000).toISOString();
}

function row(i: number, extra: Record<string, number | undefined> = {}): SensorRow {
  const r: Record<string, unknown> = { timestamp: ts(i) };
  for (const [k, v] of Object.entries(extra)) {
    if (v !== undefined) r[k] = v;
  }
  return r as SensorRow;
}

const SP = { humidity: [53, 77] as const, temperature: [20, 28] as const };

// ─── Basic spanning ──────────────────────────────────────────────────────────

describe("collapseSetpointExcursions — basic", () => {
  it("returns an empty array when all values are in range", () => {
    const rows = [row(0, { humidity: 65 }), row(1, { humidity: 70 })];
    expect(collapseSetpointExcursions(rows, ["humidity"], SP, new Set())).toHaveLength(0);
  });

  it("creates a span for a single below-range reading", () => {
    const rows = [row(0, { humidity: 29 })];
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, new Set());
    expect(spans).toHaveLength(1);
    expect(spans[0]?.channel).toBe("humidity");
    expect(spans[0]?.direction).toBe("below");
    expect(spans[0]?.worstValue).toBe(29);
    expect(spans[0]?.distanceFromRange).toBe(24); // 53-29
    expect(spans[0]?.rangeMin).toBe(53);
    expect(spans[0]?.rangeMax).toBe(77);
  });

  it("creates a span for a single above-range reading", () => {
    const rows = [row(0, { humidity: 90 })];
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, new Set());
    expect(spans[0]?.direction).toBe("above");
    expect(spans[0]?.distanceFromRange).toBe(13); // 90-77
  });

  it("collapses consecutive below-range readings into one span", () => {
    const rows = [row(0, { humidity: 29 }), row(1, { humidity: 30 }), row(2, { humidity: 31 })];
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, new Set());
    expect(spans).toHaveLength(1);
    expect(spans[0]?.startTs).toBe(ts(0));
    expect(spans[0]?.endTs).toBe(ts(2));
    expect(spans[0]?.worstValue).toBe(29); // minimum of below-range values
    expect(spans[0]?.distanceFromRange).toBe(24);
  });

  it("closes the span when a value returns to range", () => {
    const rows = [
      row(0, { humidity: 29 }),
      row(1, { humidity: 65 }), // back in range
      row(2, { humidity: 30 }),
    ];
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, new Set());
    expect(spans).toHaveLength(2);
    expect(spans[0]?.startTs).toBe(ts(0));
    expect(spans[1]?.startTs).toBe(ts(2));
  });
});

// ─── Gaps and edges ──────────────────────────────────────────────────────────

describe("collapseSetpointExcursions — gaps and edges", () => {
  it("closes a span at a gap (missing channel value)", () => {
    const rows = [
      row(0, { humidity: 29 }),
      row(1, {}), // gap — no humidity value
      row(2, { humidity: 28 }),
    ];
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, new Set());
    expect(spans).toHaveLength(2);
    expect(spans[0]?.endTs).toBe(ts(0));
    expect(spans[1]?.startTs).toBe(ts(2));
  });

  it("handles empty rows without throwing", () => {
    expect(collapseSetpointExcursions([], ["humidity"], SP, new Set())).toHaveLength(0);
  });

  it("handles a channel with no setpoint configured (measured_hd)", () => {
    const rows = [row(0, { measured_hd: 20 })];
    const spans = collapseSetpointExcursions(rows, ["measured_hd"], {}, new Set());
    expect(spans).toHaveLength(0); // no range → no spans
  });

  it("tracks the worst value across a long span", () => {
    const rows = [
      row(0, { humidity: 40 }), // distance 13
      row(1, { humidity: 25 }), // distance 28 ← worst
      row(2, { humidity: 35 }), // distance 18
    ];
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, new Set());
    expect(spans).toHaveLength(1);
    expect(spans[0]?.worstValue).toBe(25);
    expect(spans[0]?.distanceFromRange).toBe(28);
  });

  it("closes and restarts when direction changes within a span", () => {
    const rows = [
      row(0, { humidity: 29 }), // below
      row(1, { humidity: 90 }), // above
    ];
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, new Set());
    expect(spans).toHaveLength(2);
    expect(spans[0]?.direction).toBe("below");
    expect(spans[1]?.direction).toBe("above");
  });
});

// ─── alarmBelowMin ───────────────────────────────────────────────────────────

describe("collapseSetpointExcursions — alarmBelowMin", () => {
  it("does not create a below span for light (alarmBelowMin=false)", () => {
    const sp = { light: [0, 1000] as const };
    const rows = [row(0, { light: 0 }), row(1, { light: -10 })];
    expect(collapseSetpointExcursions(rows, ["light"], sp, new Set())).toHaveLength(0);
  });

  it("creates an above span for light (above max is still alarmed)", () => {
    const sp = { light: [0, 1000] as const };
    const rows = [row(0, { light: 1200 })];
    const spans = collapseSetpointExcursions(rows, ["light"], sp, new Set());
    expect(spans).toHaveLength(1);
    expect(spans[0]?.direction).toBe("above");
  });
});

// ─── Related anomaly channel ─────────────────────────────────────────────────

describe("collapseSetpointExcursions — relatedAnomalyChannel", () => {
  it("sets relatedAnomalyChannel when a coupled channel has anomalies", () => {
    const rows = [row(0, { humidity: 29 })];
    const anomalyChannels = new Set(["measured_hd"]);
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, anomalyChannels);
    expect(spans[0]?.relatedAnomalyChannel).toBe("measured_hd");
  });

  it("leaves relatedAnomalyChannel null when no coupled channel has anomalies", () => {
    const rows = [row(0, { humidity: 29 })];
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, new Set());
    expect(spans[0]?.relatedAnomalyChannel).toBeNull();
  });

  it("ignores unrelated channels' anomalies", () => {
    const rows = [row(0, { humidity: 29 })];
    const anomalyChannels = new Set(["co2"]); // co2 is not in RELATED_CHANNELS.humidity
    const spans = collapseSetpointExcursions(rows, ["humidity"], SP, anomalyChannels);
    expect(spans[0]?.relatedAnomalyChannel).toBeNull();
  });

  it("sets relatedAnomalyChannel null when there is no matching entry for the channel", () => {
    const rows = [row(0, { co2: 200 })];
    const sp = { co2: [330, 520] as const };
    const anomalyChannels = new Set(["temperature"]);
    // co2 has no RELATED_CHANNELS entry
    const spans = collapseSetpointExcursions(rows, ["co2"], sp, anomalyChannels);
    expect(spans[0]?.relatedAnomalyChannel).toBeNull();
  });
});

// ─── Multiple channels ───────────────────────────────────────────────────────

describe("collapseSetpointExcursions — multiple channels", () => {
  it("produces spans for each channel independently", () => {
    const rows = [
      row(0, { humidity: 29, temperature: 15 }),
      row(1, { humidity: 30, temperature: 16 }),
    ];
    const spans = collapseSetpointExcursions(rows, ["humidity", "temperature"], SP, new Set());
    expect(spans).toHaveLength(2);
    const channels = spans.map((s) => s.channel).sort();
    expect(channels).toEqual(["humidity", "temperature"]);
  });

  it("only produces spans for channels included in the channels list", () => {
    const rows = [row(0, { humidity: 29, temperature: 15 })];
    const spans = collapseSetpointExcursions(rows, ["temperature"], SP, new Set());
    expect(spans).toHaveLength(1);
    expect(spans[0]?.channel).toBe("temperature");
  });
});
