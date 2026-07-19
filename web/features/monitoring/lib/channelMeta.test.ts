import { describe, expect, it } from "vitest";

import {
  CHANNEL_META,
  formatSensorValue,
  getChannelStatus,
} from "@/features/monitoring/lib/channelMeta";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a setpoints map from CHANNEL_META defaults. */
function defaultSetpoints(): Record<string, readonly [number, number]> {
  return Object.fromEntries(Object.entries(CHANNEL_META).map(([ch, meta]) => [ch, meta.optimal]));
}

// ---------------------------------------------------------------------------
// CHANNEL_META shape
// ---------------------------------------------------------------------------

describe("CHANNEL_META", () => {
  it("has entries for all 7 specified channels", () => {
    const expected = [
      "temperature",
      "humidity",
      "co2",
      "light",
      "vpd",
      "soil_moisture",
      "root_zone_temp",
    ];
    for (const ch of expected) {
      expect(ch in CHANNEL_META, `missing channel: ${ch}`).toBe(true);
    }
  });

  it("marks vpd as derived and no other channel", () => {
    expect(CHANNEL_META.vpd?.provenance).toBe("derived");
    const others = Object.entries(CHANNEL_META).filter(([ch]) => ch !== "vpd");
    for (const [ch, meta] of others) {
      expect(meta.provenance, `${ch} should not be derived`).toBeUndefined();
    }
  });

  it("all optimal ranges have min < max", () => {
    for (const [ch, meta] of Object.entries(CHANNEL_META)) {
      const [min, max] = meta.optimal;
      expect(min, `${ch}: min must be less than max`).toBeLessThan(max);
    }
  });
});

// ---------------------------------------------------------------------------
// getChannelStatus — boundaries
// ---------------------------------------------------------------------------

describe("getChannelStatus — temperature (optimal [20, 28])", () => {
  const sp = defaultSetpoints();

  it("returns ok for a value inside the optimal range", () => {
    expect(getChannelStatus("temperature", 24, sp)).toBe("ok");
    expect(getChannelStatus("temperature", 20, sp)).toBe("ok");
    expect(getChannelStatus("temperature", 28, sp)).toBe("ok");
  });

  it("returns warning for a value in the widened zone (±10% of span=8 → ±0.8)", () => {
    // warnMin = 20 - 0.8 = 19.2  warnMax = 28 + 0.8 = 28.8
    expect(getChannelStatus("temperature", 19.5, sp)).toBe("warning"); // below min, above warnMin
    expect(getChannelStatus("temperature", 28.5, sp)).toBe("warning"); // above max, below warnMax
  });

  it("returns critical beyond the widened zone", () => {
    expect(getChannelStatus("temperature", 19.0, sp)).toBe("critical"); // below warnMin 19.2
    expect(getChannelStatus("temperature", 29.0, sp)).toBe("critical"); // above warnMax 28.8
  });
});

describe("getChannelStatus — boundary at exactly warnMin / warnMax", () => {
  const sp = defaultSetpoints();

  it("treats the exact warnMin boundary as critical (strictly less than)", () => {
    // temperature: span=8, pad=0.8, warnMin=19.2
    // value = 19.2 is NOT below warnMin (19.2 >= 19.2) so it falls in warning zone
    expect(getChannelStatus("temperature", 19.2, sp)).toBe("warning");
  });

  it("treats just below warnMin as critical", () => {
    expect(getChannelStatus("temperature", 19.19, sp)).toBe("critical");
  });

  it("treats value just above warnMax as critical", () => {
    // warnMax = 28.8; 28.81 > 28.8
    expect(getChannelStatus("temperature", 28.81, sp)).toBe("critical");
  });
});

// ---------------------------------------------------------------------------
// getChannelStatus — light night rule
// ---------------------------------------------------------------------------

describe("getChannelStatus — light (optimal [0, 1000])", () => {
  const sp = defaultSetpoints();

  it("never alarms below min — low light is normal at night", () => {
    expect(getChannelStatus("light", 0, sp)).toBe("ok");
    expect(getChannelStatus("light", -10, sp)).toBe("ok"); // impossible but guard holds
    expect(getChannelStatus("light", -1000, sp)).toBe("ok");
  });

  it("returns ok for values inside [0, 1000]", () => {
    expect(getChannelStatus("light", 500, sp)).toBe("ok");
    expect(getChannelStatus("light", 1000, sp)).toBe("ok");
  });

  it("returns warning when above max but within ±10% (1000 < v ≤ 1100)", () => {
    // span=1000, pad=100; warnMax=1100
    expect(getChannelStatus("light", 1050, sp)).toBe("warning");
    expect(getChannelStatus("light", 1100, sp)).toBe("warning");
  });

  it("returns critical when above warnMax (v > 1100)", () => {
    expect(getChannelStatus("light", 1101, sp)).toBe("critical");
    expect(getChannelStatus("light", 5000, sp)).toBe("critical");
  });
});

// ---------------------------------------------------------------------------
// getChannelStatus — unknown channel
// ---------------------------------------------------------------------------

describe("getChannelStatus — unknown channel", () => {
  it("returns ok when channel has no setpoint and no CHANNEL_META entry", () => {
    expect(getChannelStatus("mystery_sensor", 999, {})).toBe("ok");
    expect(getChannelStatus("mystery_sensor", 0, {})).toBe("ok");
  });

  it("uses user-provided setpoints for unknown channels", () => {
    const sp = { mystery_sensor: [10, 20] as const };
    expect(getChannelStatus("mystery_sensor", 15, sp)).toBe("ok");
    // span=10, pad=1; warnMin=9, warnMax=21
    expect(getChannelStatus("mystery_sensor", 9.5, sp)).toBe("warning");
    expect(getChannelStatus("mystery_sensor", 8, sp)).toBe("critical");
  });
});

// ---------------------------------------------------------------------------
// getChannelStatus — user setpoints override defaults
// ---------------------------------------------------------------------------

describe("getChannelStatus — user setpoint overrides", () => {
  it("uses user setpoint instead of CHANNEL_META optimal", () => {
    // Span [22,26] -> pad = 10% of 4 = 0.4, so warning zone is [21.6, 22).
    const sp = { ...defaultSetpoints(), temperature: [22, 26] as const };
    expect(getChannelStatus("temperature", 21.8, sp)).toBe("warning"); // inside pad below min=22
    expect(getChannelStatus("temperature", 21, sp)).toBe("critical"); // beyond pad
    expect(getChannelStatus("temperature", 24, sp)).toBe("ok");
  });
});

// ---------------------------------------------------------------------------
// formatSensorValue
// ---------------------------------------------------------------------------

describe("formatSensorValue", () => {
  it("formats with 0 decimals", () => {
    expect(formatSensorValue(22.7, 0)).toBe("23");
    expect(formatSensorValue(400, 0)).toBe("400");
  });

  it("formats with 1 decimal", () => {
    expect(formatSensorValue(22.75, 1)).toBe("22.8");
    expect(formatSensorValue(20, 1)).toBe("20.0");
  });

  it("formats with 2 decimals", () => {
    expect(formatSensorValue(1.234, 2)).toBe("1.23");
    expect(formatSensorValue(0.5, 2)).toBe("0.50");
  });
});
