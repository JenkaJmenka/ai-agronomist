/**
 * Pure helpers for setpoint context — used by sensor tiles (direction hints) and
 * the anomaly page (inline setpoint lines alongside detector reasoning).
 */

import { CHANNEL_META, RELATED_CHANNELS } from "@/features/monitoring/lib/channelMeta";

// ─── Tile hint ─────────────────────────────────────────────────────────────

/**
 * Short directional hint for a tile that is out of its setpoint range
 * but has NO detector anomaly.  Returns `null` when:
 *   - the value is within range (no hint needed)
 *   - no range is configured (`no_range` tiles are neutral)
 *   - the channel suppresses below-min alarms (e.g. light at night)
 *
 * Examples: `"below setpoint 53–77"`, `"above setpoint 20–28"`
 */
export function formatSetpointHint(
  channel: string,
  value: number | null,
  setpoints: Readonly<Record<string, readonly [number, number]>>,
): string | null {
  if (value === null) return null;
  const range = setpoints[channel] ?? CHANNEL_META[channel]?.optimal;
  if (!range) return null;
  const [min, max] = range;
  if (value < min) {
    if (CHANNEL_META[channel]?.alarmBelowMin === false) return null;
    return `below setpoint ${min}–${max}`;
  }
  if (value > max) {
    return `above setpoint ${min}–${max}`;
  }
  return null;
}

// ─── Anomaly page context ───────────────────────────────────────────────────

/**
 * Full setpoint context line for the anomaly page — shown alongside detector
 * reasoning for every flagged channel.
 *
 * The **within-range case is the most important one**: it is the "all-green
 * paradox" where Priva shows no alarm but the LSTM-VAE did detect something.
 * It must be explicit, not absent.
 *
 * Examples:
 *  - `"29 % — 24 below setpoint 53–77"`
 *  - `"14.2 g/m³ — no setpoint range configured"`
 *  - `"24.0 °C — within setpoint 20–28"`
 */
export function formatSetpointContext(
  channel: string,
  value: number | null,
  setpoints: Readonly<Record<string, readonly [number, number]>>,
): string {
  const meta = CHANNEL_META[channel];
  const unit = meta?.unit ?? "";
  const decimals = meta?.decimals ?? 2;
  const range = setpoints[channel] ?? meta?.optimal;

  const valStr =
    value !== null ? `${value.toFixed(decimals)}${unit !== "" ? ` ${unit}` : ""}` : "—";

  if (!range) {
    return `${valStr} — no setpoint range configured`;
  }

  const [min, max] = range;

  if (value === null) {
    return `no value — setpoint ${min}–${max}`;
  }

  if (value < min) {
    // Channels with alarmBelowMin=false (e.g. light) are not truly "below range"
    // in an alarm sense — annotate honestly rather than claiming it's within range.
    if (CHANNEL_META[channel]?.alarmBelowMin === false) {
      return `${valStr} — within setpoint ${min}–${max} (below-min not alarmed)`;
    }
    const dist = (min - value).toFixed(decimals);
    return `${valStr} — ${dist} below setpoint ${min}–${max}`;
  }

  if (value > max) {
    const dist = (value - max).toFixed(decimals);
    return `${valStr} — ${dist} above setpoint ${min}–${max}`;
  }

  return `${valStr} — within setpoint ${min}–${max}`;
}

// ─── Related-channel hint ───────────────────────────────────────────────────

/**
 * For a tile that is out of setpoint range but has NO anomaly, return the first
 * related channel (from `RELATED_CHANNELS`) that has detector anomalies on the page.
 *
 * Returns the channel name (not its display label) so callers can localise.
 * Returns `null` when no related channel with anomalies exists.
 *
 * This never changes the anomaly count or badge on the related tile — it is a
 * hint only.
 */
export function getRelatedAnomalyChannel(
  channel: string,
  anomalyCounts: ReadonlyMap<string, number>,
): string | null {
  const related = RELATED_CHANNELS[channel];
  if (!related) return null;
  for (const relCh of related) {
    const count = anomalyCounts.get(relCh);
    if (count !== undefined && count > 0) return relCh;
  }
  return null;
}
