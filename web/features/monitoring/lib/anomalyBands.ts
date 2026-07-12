import type { SensorRow } from "@/features/monitoring/api/readings";

/** Inclusive [start, end] band in unix seconds (uPlot's x unit). */
export type AnomalyBand = readonly [number, number];

/** Half-width floor so a single flagged point still paints a visible band. */
const MIN_BAND_HALF_WIDTH_S = 60;

function isFlagged(row: SensorRow, threshold: number): boolean {
  return typeof row.anomaly_score === "number" && row.anomaly_score >= threshold;
}

/**
 * Contiguous runs of rows scoring at/above the threshold, padded by half the
 * sampling step on each side so bands cover the anomalous region rather than
 * hairline-marking single samples. Rows must be timestamp-ordered (they are —
 * the readings endpoints return them sorted).
 */
export function computeAnomalyBands(rows: SensorRow[], threshold: number): AnomalyBand[] {
  if (rows.length === 0) return [];

  const timesSec = rows.map((row) => new Date(row.timestamp).getTime() / 1000);
  const lastTime = timesSec.at(-1) ?? timesSec[0];
  const avgStep = rows.length > 1 ? (lastTime - timesSec[0]) / (rows.length - 1) : 0;
  const halfWidth = Math.max(avgStep / 2, MIN_BAND_HALF_WIDTH_S);

  const bands: AnomalyBand[] = [];
  let runStart = -1;
  for (let i = 0; i < rows.length; i++) {
    if (isFlagged(rows[i], threshold)) {
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
