import { describe, expect, it } from "vitest";

import { countAnomaliesByChannel } from "@/features/monitoring/lib/tileAnomalies";

const CHANNELS = ["temperature", "humidity", "heating_temp", "root_zone_temp"];

describe("countAnomaliesByChannel", () => {
  it("counts only the channels the detector attributed", () => {
    const map = new Map<string, readonly string[]>([
      ["2026-10-01T10:00:00+00:00", ["heating_temp"]],
      ["2026-10-01T10:05:00+00:00", ["heating_temp"]],
    ]);
    const counts = countAnomaliesByChannel(map, CHANNELS);
    expect(counts.get("heating_temp")).toBe(2);
    expect(counts.has("humidity")).toBe(false);
    expect(counts.has("temperature")).toBe(false);
  });

  it("de-duplicates the raw + normalised ISO keys of the same instant", () => {
    const map = new Map<string, readonly string[]>([
      ["2026-10-01T10:00:00.000000+00:00", ["humidity"]],
      ["2026-10-01T10:00:00.000Z", ["humidity"]],
    ]);
    expect(countAnomaliesByChannel(map, CHANNELS).get("humidity")).toBe(1);
  });

  it("never substring-matches (temperature must not badge root_zone_temp)", () => {
    const map = new Map<string, readonly string[]>([["2026-10-01T10:00:00Z", ["temperature"]]]);
    const counts = countAnomaliesByChannel(map, CHANNELS);
    expect(counts.get("temperature")).toBe(1);
    expect(counts.has("root_zone_temp")).toBe(false);
  });

  it("does not badge gas_co2 for a climate co2 anomaly on the Gas page", () => {
    const map = new Map<string, readonly string[]>([["2026-10-01T10:00:00Z", ["co2"]]]);
    expect(countAnomaliesByChannel(map, ["gas_co2", "nox"]).size).toBe(0);
  });

  it("maps raw Priva sensor names through the display aliases", () => {
    const map = new Map<string, readonly string[]>([
      ["2026-10-01T10:00:00Z", ["measured_relative_humidity"]],
    ]);
    expect(countAnomaliesByChannel(map, CHANNELS).get("humidity")).toBe(1);
  });

  it("credits every sensor of a multi-sensor anomaly", () => {
    const map = new Map<string, readonly string[]>([
      ["2026-10-01T10:00:00Z", ["temperature", "humidity"]],
    ]);
    const counts = countAnomaliesByChannel(map, CHANNELS);
    expect(counts.get("temperature")).toBe(1);
    expect(counts.get("humidity")).toBe(1);
  });

  it("ignores sensors that are not on this page", () => {
    const map = new Map<string, readonly string[]>([["2026-10-01T10:00:00Z", ["nox"]]]);
    expect(countAnomaliesByChannel(map, CHANNELS).size).toBe(0);
  });
});
