import { describe, expect, it } from "vitest";

import type { SensorRow } from "@/features/monitoring/api/readings";
import {
  anomalousChannelsForRow,
  isAnomalousRow,
  resolveAffectedChannels,
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

describe("resolveAffectedChannels", () => {
  it("maps detector names onto visible table columns", () => {
    expect(
      resolveAffectedChannels(["temperature", "humidity"], ["temperature", "humidity", "co2"]),
    ).toEqual(["temperature", "humidity"]);
  });

  it("soft-matches partial sensor names", () => {
    expect(resolveAffectedChannels(["temp"], ["temperature", "humidity"])).toEqual(["temperature"]);
  });
});

describe("anomalousChannelsForRow", () => {
  const channels = ["temperature", "humidity", "co2"];

  it("returns nothing for a normal row", () => {
    expect(anomalousChannelsForRow(row({ timestamp: "t1" }), channels, 0.5, SETPOINTS)).toEqual([]);
  });

  it("highlights only detector-affected sensors when provided", () => {
    expect(
      anomalousChannelsForRow(
        row({ timestamp: "t1", anomaly_score: 0.9, temperature: 24, humidity: 60 }),
        channels,
        0.5,
        SETPOINTS,
        ["humidity"],
      ),
    ).toEqual(["humidity"]);
  });

  it("does not fall back to every channel when affected list is empty", () => {
    // Flagged but in-band and no attribution → score cell only (no sensor cells).
    expect(
      anomalousChannelsForRow(
        row({ timestamp: "t1", anomaly_score: 0.9, temperature: 24, humidity: 60, co2: 400 }),
        channels,
        0.5,
        SETPOINTS,
        [],
      ),
    ).toEqual([]);
  });

  it("falls back to off-setpoint channels when attribution is missing", () => {
    expect(
      anomalousChannelsForRow(
        row({ timestamp: "t1", anomaly_score: 0.9, temperature: 35 }),
        channels,
        0.5,
        SETPOINTS,
        null,
      ),
    ).toEqual(["temperature"]);
  });
});
