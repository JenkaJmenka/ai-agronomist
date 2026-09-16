import { describe, expect, it } from "vitest";

import type { SensorRow } from "@/features/monitoring/api/readings";
import { computeAnomalyBands } from "@/features/monitoring/lib/anomalyBands";

const HOUR_S = 3600;
const BASE_MS = new Date("2026-06-01T00:00:00Z").getTime();
const BASE_S = BASE_MS / 1000;

function makeRows(scores: (number | null)[]): SensorRow[] {
  return scores.map((score, i) => ({
    timestamp: new Date(BASE_MS + i * HOUR_S * 1000).toISOString(),
    temperature: 22,
    anomaly_score: score,
  }));
}

describe("computeAnomalyBands", () => {
  it("returns no bands when nothing is at or above the threshold", () => {
    expect(computeAnomalyBands(makeRows([0.01, 0.05, null]), 0.15)).toEqual([]);
    expect(computeAnomalyBands([], 0.15)).toEqual([]);
  });

  it("merges a contiguous flagged run into one padded band", () => {
    const bands = computeAnomalyBands(makeRows([0.01, 0.9, 0.9, 0.01]), 0.15);
    // Hourly step → half-step padding of 1800 s around rows 1..2.
    expect(bands).toEqual([[BASE_S + HOUR_S - 1800, BASE_S + 2 * HOUR_S + 1800]]);
  });

  it("emits separate bands for separate runs, including a run at the end", () => {
    const bands = computeAnomalyBands(makeRows([0.9, 0.01, 0.01, 0.9]), 0.15);
    expect(bands).toHaveLength(2);
    expect(bands[0][1]).toBeLessThan(bands[1][0]);
    expect(bands[1][1]).toBe(BASE_S + 3 * HOUR_S + 1800);
  });

  it("ignores rows with missing scores and pads single points to a visible width", () => {
    const bands = computeAnomalyBands(makeRows([null, 0.9, null]), 0.15);
    expect(bands).toHaveLength(1);
    const [start, end] = bands[0];
    expect(end - start).toBeGreaterThanOrEqual(120);
  });

  it("returns no bands when highlight is disabled (wrong page / sensors off)", () => {
    expect(computeAnomalyBands(makeRows([0.9, 0.9]), 0.15, { enabled: false })).toEqual([]);
  });

  it("keeps only runs whose affected sensors intersect focusVisible", () => {
    const rows: SensorRow[] = [
      { timestamp: new Date(BASE_MS).toISOString(), anomaly_score: 0.9, temperature: 35 },
      {
        timestamp: new Date(BASE_MS + HOUR_S * 1000).toISOString(),
        anomaly_score: 0.9,
        humidity: 90,
      },
      {
        timestamp: new Date(BASE_MS + 2 * HOUR_S * 1000).toISOString(),
        anomaly_score: 0.01,
        temperature: 22,
      },
    ];
    const affected = new Map<string, readonly string[]>([
      [rows[0].timestamp, ["temperature"]],
      [rows[1].timestamp, ["humidity"]],
    ]);
    const bands = computeAnomalyBands(rows, 0.15, {
      focusVisible: ["temperature"],
      affectedByTimestamp: affected,
    });
    expect(bands).toHaveLength(1);
    expect(bands[0][0]).toBeLessThan(BASE_S + 1);
  });

  it("keeps score-based bands when focus matches none of the attributed sensors", () => {
    // Episode jump carries channel=measured_hd, detector attributes
    // temperature/humidity — bands used to vanish once enrichment landed.
    const rows = makeRows([0.9, 0.9, 0.01]);
    const affected = new Map<string, readonly string[]>([
      [rows[0].timestamp, ["temperature", "humidity"]],
      [rows[1].timestamp, ["temperature", "humidity"]],
    ]);
    const bands = computeAnomalyBands(rows, 0.15, {
      focusVisible: ["measured_hd"],
      affectedByTimestamp: affected,
    });
    expect(bands).toHaveLength(1);
    expect(computeAnomalyBands(rows, 0.15)).toEqual(bands);
  });
});
