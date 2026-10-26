import { toDisplayChannel } from "@/features/monitoring/api/readings";

/**
 * Count detector-flagged timestamps per page channel.
 *
 * Input is MonitoringView's `affectedByTimestamp` (enrichment + live SSE),
 * which stores each anomaly under both its raw and normalised ISO key — so
 * timestamps are de-duplicated by epoch ms before counting.
 *
 * Detector sensor names go through the same display aliases as the readings
 * (`measured_relative_humidity` → `humidity`) and must then match a page
 * channel EXACTLY. No substring fallback: on the Gas page a climate `co2`
 * anomaly must not badge `gas_co2`, nor `temperature` badge `root_zone_temp`.
 */
export function countAnomaliesByChannel(
  affectedByTimestamp: ReadonlyMap<string, readonly string[]>,
  channels: readonly string[],
): ReadonlyMap<string, number> {
  const onPage = new Set(channels);
  const seen = new Map<string, Set<number | string>>();
  for (const [ts, sensors] of affectedByTimestamp) {
    const ms = Date.parse(ts);
    const key: number | string = Number.isFinite(ms) ? ms : ts;
    for (const sensor of sensors) {
      const ch = toDisplayChannel(sensor);
      if (!onPage.has(ch)) continue;
      let set = seen.get(ch);
      if (set === undefined) {
        set = new Set();
        seen.set(ch, set);
      }
      set.add(key);
    }
  }
  const counts = new Map<string, number>();
  for (const [ch, set] of seen) counts.set(ch, set.size);
  return counts;
}
