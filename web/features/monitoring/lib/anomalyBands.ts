import type { SensorRow } from "@/features/monitoring/api/readings";

/** Inclusive [start, end] band in unix seconds (uPlot's x unit). */
export type AnomalyBand = readonly [number, number];

/** Half-width floor so a single flagged point still paints a visible band. */
const MIN_BAND_HALF_WIDTH_S = 60;

export interface AnomalyBandOptions {
  /**
   * When false, no bands — used when jump ``channel=`` sensors are inactive
   * or absent on Climate/Gas.
   */
  readonly enabled?: boolean;
  /**
   * Jump-focus channels that are currently visible. When non-empty, a row
   * only bands if detector-affected sensors intersect this set (or if
   * attribution is missing, score≥threshold is enough while focus is active).
   */
  readonly focusVisible?: readonly string[];
  /** Detector-attributed sensors keyed by reading timestamp. */
  readonly affectedByTimestamp?: ReadonlyMap<string, readonly string[]>;
}

function timestampKey(ts: string): string {
  const ms = Date.parse(ts);
  return Number.isFinite(ms) ? new Date(ms).toISOString() : ts;
}

function isScoreFlagged(row: SensorRow, threshold: number): boolean {
  return typeof row.anomaly_score === "number" && row.anomaly_score >= threshold;
}

function affectedForRow(
  row: SensorRow,
  affectedByTimestamp: ReadonlyMap<string, readonly string[]> | undefined,
): readonly string[] | null {
  if (!affectedByTimestamp) return null;
  return (
    affectedByTimestamp.get(row.timestamp) ??
    affectedByTimestamp.get(timestampKey(row.timestamp)) ??
    null
  );
}

function isFlagged(row: SensorRow, threshold: number, options?: AnomalyBandOptions): boolean {
  if (!isScoreFlagged(row, threshold)) return false;
  const focus = options?.focusVisible;
  if (!focus || focus.length === 0) return true;

  const affected = affectedForRow(row, options?.affectedByTimestamp);
  if (!affected || affected.length === 0) return true;

  const focusSet = new Set(focus.map((ch) => ch.toLowerCase()));
  return affected.some((sensor) => {
    const s = sensor.toLowerCase();
    if (focusSet.has(s)) return true;
    for (const ch of focusSet) {
      if (ch.includes(s) || s.includes(ch)) return true;
    }
    return false;
  });
}

/**
 * Contiguous runs of rows scoring at/above the threshold, padded by half the
 * sampling step on each side so bands cover the anomalous region rather than
 * hairline-marking single samples. Rows must be timestamp-ordered (they are —
 * the readings endpoints return them sorted).
 */
export function computeAnomalyBands(
  rows: SensorRow[],
  threshold: number,
  options?: AnomalyBandOptions,
): AnomalyBand[] {
  if (options?.enabled === false) return [];
  if (rows.length === 0) return [];

  const timesSec = rows.map((row) => new Date(row.timestamp).getTime() / 1000);
  const lastTime = timesSec.at(-1) ?? timesSec[0];
  const avgStep = rows.length > 1 ? (lastTime - timesSec[0]) / (rows.length - 1) : 0;
  const halfWidth = Math.max(avgStep / 2, MIN_BAND_HALF_WIDTH_S);

  const bands: AnomalyBand[] = [];
  let runStart = -1;
  for (let i = 0; i < rows.length; i++) {
    if (isFlagged(rows[i], threshold, options)) {
      if (runStart === -1) runStart = i;
      continue;
    }
    if (runStart !== -1) {
      bands.push([timesSec[runStart] - halfWidth, timesSec[i - 1] + halfWidth]);
      runStart = -1;
    }
  }
  if (runStart !== -1) {
    bands.push([timesSec[runStart] - halfWidth, lastTime + halfWidth]);
  }
  return bands;
}
