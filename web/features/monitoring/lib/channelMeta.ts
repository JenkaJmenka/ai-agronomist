/**
 * Channel metadata — single source of truth for labels, units, decimals, and
 * optimal ranges. All UI components derive display and alarm logic from here.
 */

export interface ChannelMeta {
  readonly label: string;
  readonly unit: string;
  readonly decimals: number;
  /** [min, max] optimal/setpoint range (best-practice defaults). */
  readonly optimal: readonly [number, number];
  /**
   * Present when the channel value is calculated from other sensors rather
   * than measured directly (e.g. VPD from temperature + humidity).
   */
  readonly provenance?: "derived";
}

export type ChannelStatus = "ok" | "warning" | "critical";

export const CHANNEL_META: Readonly<Record<string, ChannelMeta>> = {
  temperature: {
    label: "Air Temp",
    unit: "°C",
    decimals: 1,
    optimal: [20, 28],
  },
  humidity: {
    label: "Humidity",
    unit: "%",
    decimals: 0,
    optimal: [55, 75],
  },
  co2: {
    label: "CO₂",
    unit: "ppm",
    decimals: 0,
    optimal: [350, 500],
  },
  light: {
    label: "Light",
    unit: "lux",
    decimals: 0,
    // min=0 here but below-min is never alarmed — nighttime is normal.
    // Only values above max trigger warning/critical.
    optimal: [0, 1000],
  },
  vpd: {
    label: "VPD",
    unit: "kPa",
    decimals: 2,
    optimal: [0.5, 1.5],
    provenance: "derived",
  },
  soil_moisture: {
    label: "Soil Moisture",
    unit: "%",
    decimals: 0,
    optimal: [60, 85],
  },
  root_zone_temp: {
    label: "Root Zone",
    unit: "°C",
    decimals: 1,
    optimal: [18, 24],
  },
} as const;

/**
 * Determine the channel status given its current value and effective setpoints.
 *
 * Rules:
 * - Within [min, max] → "ok"
 * - Within the range widened by 10% of its span on each side → "warning"
 * - Beyond the widened range → "critical"
 *
 * Special case for `light`:
 *   Below min is never non-ok (nighttime is normal). Only above max is alarmed.
 *
 * Unknown channels (not in CHANNEL_META and no user setpoint) → always "ok".
 */
export function getChannelStatus(
  channel: string,
  value: number,
  setpoints: Readonly<Record<string, readonly [number, number]>>,
): ChannelStatus {
  const range = setpoints[channel] ?? CHANNEL_META[channel]?.optimal;
  if (range === undefined) return "ok";

  const [min, max] = range;
  const span = max - min;
  const pad = span * 0.1;
  const warnMin = min - pad;
  const warnMax = max + pad;

  if (value < min) {
    // Light: low values are normal during night — never alarm.
    if (channel === "light") return "ok";
    if (value < warnMin) return "critical";
    return "warning";
  }

  if (value > max) {
    if (value > warnMax) return "critical";
    return "warning";
  }

  return "ok";
}

/** Format a numeric sensor value for display (rounds to meta.decimals). */
export function formatSensorValue(value: number, decimals: number): string {
  return value.toFixed(decimals);
}
