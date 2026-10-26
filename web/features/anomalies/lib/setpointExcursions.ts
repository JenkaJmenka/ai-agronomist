/**
 * Pure function that collapses consecutive out-of-setpoint-range sensor readings
 * into contiguous spans.  Used by the Anomaly Detection page's "Setpoint
 * excursions the detector did NOT flag" section.
 */

import type { SensorRow } from "@/features/monitoring/api/readings";
import { CHANNEL_META, RELATED_CHANNELS } from "@/features/monitoring/lib/channelMeta";

/** A contiguous run of readings where one channel was outside its setpoint range. */
export interface SetpointExcursionSpan {
  readonly channel: string;
  readonly startTs: string;
  readonly endTs: string;
  /** The worst (most deviant) value seen during the span. */
  readonly worstValue: number;
  readonly direction: "below" | "above";
  /** Distance between the worst value and the range boundary. */
  readonly distanceFromRange: number;
  readonly rangeMin: number;
  readonly rangeMax: number;
  /**
   * A related channel (from RELATED_CHANNELS) that has detector anomalies in the
   * same window.  When set, the span notes "likely the same event as the X anomaly".
   * This is a hint only — it never adds a badge or count to the anomaly tile.
   */
  readonly relatedAnomalyChannel: string | null;
}

/** Whether a channel value constitutes an excursion given its range. */
function excursionDirection(
  channel: string,
  value: number,
  min: number,
  max: number,
): "below" | "above" | null {
  if (value < min) {
    if (CHANNEL_META[channel]?.alarmBelowMin === false) return null;
    return "below";
  }
  if (value > max) return "above";
  return null;
}

/** Best-worst value given a direction (largest distance from boundary). */
function isWorse(candidate: number, current: number, direction: "below" | "above"): boolean {
  return direction === "below" ? candidate < current : candidate > current;
}

function findRelatedAnomalyChannel(
  channel: string,
  anomalyChannels: ReadonlySet<string>,
): string | null {
  const related = RELATED_CHANNELS[channel];
  if (!related) return null;
  for (const relCh of related) {
    if (anomalyChannels.has(relCh)) return relCh;
  }
  return null;
}

interface OpenSpan {
  startTs: string;
  endTs: string;
  worstValue: number;
  direction: "below" | "above";
  distanceFromRange: number;
}

/**
 * Collapse consecutive out-of-setpoint-range readings into spans.
 *
 * Rules:
 * - Channels with `alarmBelowMin=false` (e.g. light) never produce a "below" span.
 * - A gap row (channel value missing/non-numeric) closes any open span.
 * - Direction changes (below → above) close and restart the span.
 * - `anomalyChannels` is the set of channels that have ANY detector anomaly in this
 *   window.  When a coupled channel (RELATED_CHANNELS) is in this set, the span
 *   records it as `relatedAnomalyChannel`.
 */
export function collapseSetpointExcursions(
  rows: readonly SensorRow[],
  channels: readonly string[],
  setpoints: Readonly<Record<string, readonly [number, number]>>,
  anomalyChannels: ReadonlySet<string>,
): SetpointExcursionSpan[] {
  const result: SetpointExcursionSpan[] = [];

  for (const channel of channels) {
    const range = setpoints[channel] ?? CHANNEL_META[channel]?.optimal;
    if (!range) continue; // no setpoint → cannot detect excursions for this channel

    const [min, max] = range;
    let open: OpenSpan | null = null;

    const closeSpan = (): void => {
      if (!open) return;
      result.push({
        channel,
        startTs: open.startTs,
        endTs: open.endTs,
        worstValue: open.worstValue,
        direction: open.direction,
        distanceFromRange: open.distanceFromRange,
        rangeMin: min,
        rangeMax: max,
        relatedAnomalyChannel: findRelatedAnomalyChannel(channel, anomalyChannels),
      });
      open = null;
    };

    for (const row of rows) {
      const raw = row[channel];
      if (typeof raw !== "number") {
        closeSpan();
        continue;
      }
      const dir = excursionDirection(channel, raw, min, max);
      if (dir === null) {
        closeSpan();
        continue;
      }

      const distance = dir === "below" ? min - raw : raw - max;

      if (open === null) {
        open = {
          startTs: row.timestamp,
          endTs: row.timestamp,
          worstValue: raw,
          direction: dir,
          distanceFromRange: distance,
        };
      } else if (open.direction === dir) {
        open.endTs = row.timestamp;
        if (isWorse(raw, open.worstValue, dir)) {
          open.worstValue = raw;
          open.distanceFromRange = distance;
        }
      } else {
        // Direction changed mid-span (rare but possible) — close and restart.
        closeSpan();
        open = {
          startTs: row.timestamp,
          endTs: row.timestamp,
          worstValue: raw,
          direction: dir,
          distanceFromRange: distance,
        };
      }
    }

    closeSpan();
  }

  return result;
}
