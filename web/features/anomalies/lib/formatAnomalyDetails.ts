/** Format helpers for anomaly group row details (sensors / values / reasoning). */

const SENSOR_VALUE_KEYS = [
  "temperature",
  "humidity",
  "co2",
  "light",
  "vpd",
  "soil_moisture",
  "root_zone_temp",
] as const;

export function formatSensorValues(
  values: Readonly<Record<string, unknown>>,
  sensors: readonly string[],
): string {
  const keys = sensors.length > 0 ? sensors : SENSOR_VALUE_KEYS;
  const parts: string[] = [];
  for (const key of keys) {
    const raw = values[key];
    if (typeof raw !== "number" || !Number.isFinite(raw)) continue;
    parts.push(`${key}=${Number.isInteger(raw) ? String(raw) : raw.toFixed(2)}`);
  }
  return parts.join(", ");
}

export function formatSensorList(sensors: readonly string[]): string {
  return sensors.length > 0 ? sensors.join(", ") : "—";
}
