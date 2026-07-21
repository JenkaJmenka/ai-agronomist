import { describe, expect, it } from "vitest";

import type { SensorRow } from "@/features/monitoring/api/readings";
import {
  anomalousChannelsForRow,
  isAnomalousRow,
} from "@/features/monitoring/components/ReadingsTable";
import { CHANNEL_META } from "@/features/monitoring/lib/channelMeta";

const SETPOINTS = Object.fromEntries(
  Object.entries(CHANNEL_META).map(([ch, meta]) => [ch, meta.optimal]),
);

function row(partial: Partial<SensorRow> & { timestamp: string }): SensorRow {
  return {
    temperature: 24,
    humidity: 60,
    co2: 400,
    anomaly_score: 0.01,
    ...partial,
  };
}

describe("isAnomalousRow", () => {
  it("flags rows at or above the threshold", () => {
    expect(isAnomalousRow(row({ timestamp: "t1", anomaly_score: 0.5 }), 0.5)).toBe(true);
    expect(isAnomalousRow(row({ timestamp: "t1", anomaly_score: 0.49 }), 0.5)).toBe(false);
    expect(isAnomalousRow(row({ timestamp: "t1", anomaly_score: null }), 0.5)).toBe(false);
  });
});

describe("anomalousChannelsForRow", () => {
  const channels = ["temperature", "humidity", "co2"];

  it("returns nothing for a normal row", () => {
    expect(
      anomalousChannelsForRow(row({ timestamp: "t1" }), channels, 0.5, SETPOINTS),
    ).toEqual([]);
  });

  it("prefers off-setpoint channels on a flagged row", () => {
    expect(
      anomalousChannelsForRow(
        row({ timestamp: "t1", anomaly_score: 0.9, temperature: 35 }),
        channels,
        0.5,
        SETPOINTS,
      ),
    ).toEqual(["temperature"]);
  });

  it("falls back to all numeric channels when flagged but still in band", () => {
    // Score fired (e.g. pattern/SCADA) while values sit inside monitoring setpoints.
    expect(
      anomalousChannelsForRow(
        row({ timestamp: "t1", anomaly_score: 0.9, temperature: 24, humidity: 60, co2: 400 }),
        channels,
        0.5,
        SETPOINTS,
      ),
    ).toEqual(["temperature", "humidity", "co2"]);
  });
});
