import { describe, expect, it } from "vitest";

import {
  formatSetpointContext,
  formatSetpointHint,
  getRelatedAnomalyChannel,
} from "@/features/monitoring/lib/setpointContext";

// ─── formatSetpointHint ─────────────────────────────────────────────────────

describe("formatSetpointHint", () => {
  const sp: Record<string, readonly [number, number]> = {
    humidity: [53, 77],
    temperature: [20, 28],
  };

  it("returns null when the value is within range", () => {
    expect(formatSetpointHint("humidity", 65, sp)).toBeNull();
    expect(formatSetpointHint("humidity", 53, sp)).toBeNull();
    expect(formatSetpointHint("humidity", 77, sp)).toBeNull();
  });

  it("returns a below-setpoint hint when below min", () => {
    expect(formatSetpointHint("humidity", 29, sp)).toBe("below setpoint 53–77");
  });

  it("returns an above-setpoint hint when above max", () => {
    expect(formatSetpointHint("humidity", 90, sp)).toBe("above setpoint 53–77");
  });

  it("returns null when value is null", () => {
    expect(formatSetpointHint("humidity", null, sp)).toBeNull();
  });

  it("returns null when no range is configured", () => {
    expect(formatSetpointHint("measured_hd", 14, {})).toBeNull();
  });

  it("returns null for light below min (alarmBelowMin=false)", () => {
    const lightSp = { light: [0, 1000] as const };
    expect(formatSetpointHint("light", -5, lightSp)).toBeNull();
    expect(formatSetpointHint("light", 0, lightSp)).toBeNull();
  });

  it("returns above-setpoint hint for light above max", () => {
    const lightSp = { light: [0, 1000] as const };
    expect(formatSetpointHint("light", 1200, lightSp)).toBe("above setpoint 0–1000");
  });

  it("uses CHANNEL_META optimal when setpoints map is empty", () => {
    expect(formatSetpointHint("temperature", 15, {})).toBe("below setpoint 20–28");
  });

  it("user setpoint overrides CHANNEL_META", () => {
    const overrideSp = { humidity: [60, 80] as const };
    expect(formatSetpointHint("humidity", 55, overrideSp)).toBe("below setpoint 60–80");
  });
});

// ─── formatSetpointContext ──────────────────────────────────────────────────

describe("formatSetpointContext", () => {
  const sp: Record<string, readonly [number, number]> = { humidity: [53, 77] };

  it("produces a below-range line with distance", () => {
    // humidity: 0 decimals, unit="%", below by 53-29=24
    expect(formatSetpointContext("humidity", 29, sp)).toBe("29 % — 24 below setpoint 53–77");
  });

  it("produces an above-range line with distance", () => {
    expect(formatSetpointContext("humidity", 90, sp)).toBe("90 % — 13 above setpoint 53–77");
  });

  it("produces a within-range line (all-green-paradox case)", () => {
    expect(formatSetpointContext("humidity", 65, sp)).toBe("65 % — within setpoint 53–77");
  });

  it("produces a no-setpoint line for measured_hd", () => {
    expect(formatSetpointContext("measured_hd", 14.2, {})).toBe(
      "14.2 g/m³ — no setpoint range configured",
    );
  });

  it("handles null value with a range configured", () => {
    expect(formatSetpointContext("humidity", null, sp)).toBe("no value — setpoint 53–77");
  });

  it("handles null value with no range configured", () => {
    expect(formatSetpointContext("measured_hd", null, {})).toBe("— — no setpoint range configured");
  });

  it("respects decimal precision: temperature has 1 decimal", () => {
    const tSp = { temperature: [20, 28] as const };
    // 0.7 below 20 = 0.7
    expect(formatSetpointContext("temperature", 19.3, tSp)).toBe(
      "19.3 °C — 0.7 below setpoint 20–28",
    );
  });

  it("marks light-below-min as not-alarmed rather than below-range", () => {
    // value=0 is below the min (100), but light has alarmBelowMin=false in CHANNEL_META
    // so we annotate "within setpoint" rather than "below setpoint".
    const lightSp = { light: [100, 1000] as const };
    expect(formatSetpointContext("light", 0, lightSp)).toContain("within setpoint");
    expect(formatSetpointContext("light", 0, lightSp)).toContain("below-min not alarmed");
  });

  it("falls back to CHANNEL_META optimal when setpoints map omits the channel", () => {
    // humidity optimal is [53,77], value 65 is within range
    expect(formatSetpointContext("humidity", 65, {})).toContain("within setpoint");
  });
});

// ─── getRelatedAnomalyChannel ───────────────────────────────────────────────

describe("getRelatedAnomalyChannel", () => {
  it("returns the first related channel that has anomalies", () => {
    const counts = new Map([["measured_hd", 3]]);
    expect(getRelatedAnomalyChannel("humidity", counts)).toBe("measured_hd");
  });

  it("returns null when no related channel has anomalies", () => {
    const counts = new Map<string, number>();
    expect(getRelatedAnomalyChannel("humidity", counts)).toBeNull();
  });

  it("returns null for a channel with no related channels", () => {
    const counts = new Map([["temperature", 1]]);
    expect(getRelatedAnomalyChannel("co2", counts)).toBeNull();
  });

  it("returns null when the related channel's count is zero", () => {
    const counts = new Map([["measured_hd", 0]]);
    expect(getRelatedAnomalyChannel("humidity", counts)).toBeNull();
  });

  it("returns vpd as a related channel for humidity when it has anomalies", () => {
    const counts = new Map([["vpd", 2]]);
    expect(getRelatedAnomalyChannel("humidity", counts)).toBe("vpd");
  });

  it("prefers measured_hd over vpd when both have anomalies (RELATED_CHANNELS order)", () => {
    // RELATED_CHANNELS.humidity = ["measured_hd", "vpd"]
    const counts = new Map([
      ["measured_hd", 1],
      ["vpd", 2],
    ]);
    expect(getRelatedAnomalyChannel("humidity", counts)).toBe("measured_hd");
  });

  it("returns a related channel for temperature → vpd", () => {
    const counts = new Map([["vpd", 5]]);
    expect(getRelatedAnomalyChannel("temperature", counts)).toBe("vpd");
  });
});
