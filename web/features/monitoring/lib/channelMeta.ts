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
  /**
   * When `false`, values below the optimal minimum are never alarmed.
   * Used for light — low values are normal during nighttime.
   * Absent (undefined) means below-min triggers warning/critical as normal.
   */
  readonly alarmBelowMin?: false;
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
    // Widened from [55,75] — temperature coupling can graze the 55 floor causing
    // routine warning flicker during demos with no real anomaly present.
    optimal: [53, 77],
  },
  co2: {
    label: "CO₂",
    unit: "ppm",
    decimals: 0,
    // Widened from [350,500] — midday dip bottoms near 350 causing routine
    // warning flicker during demos with no real anomaly present.
    optimal: [330, 520],
  },
  light: {
    label: "Light",
    unit: "lux",
    decimals: 0,
    // min=0 here but below-min is never alarmed — nighttime is normal.
    // alarmBelowMin=false drives this rule via metadata rather than a string
    // literal so any future light-like channel can reuse the same behaviour.
    optimal: [0, 1000],
    alarmBelowMin: false,
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
    // Channels with alarmBelowMin=false treat low values as normal (e.g. light at night).
    if (CHANNEL_META[channel]?.alarmBelowMin === false) return "ok";
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
