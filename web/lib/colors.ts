/**
 * Okabe-Ito colorblind-safe categorical palette (plan §7.1).
 *
 * The 8th slot is black in the original Okabe-Ito set, which measures 1.12:1
 * against the dark-theme background — an invisible series. Neutral grey holds
 * 4.33:1 on light and 4.13:1 on dark while staying distinguishable from the
 * seven hues.
 */
export const CHART_PALETTE = [
  "#0072b2", // blue
  "#e69f00", // orange
  "#009e73", // green
  "#cc79a7", // pink
  "#56b4e9", // sky
  "#d55e00", // vermillion
  "#f0e442", // yellow
  "#767676", // neutral grey — Okabe-Ito black, lightened for dark mode
] as const;

export function channelColor(index: number): string {
  return CHART_PALETTE[index % CHART_PALETTE.length];
}

/** Translucent --sev-critical (#ef4444) — anomaly band overlay on charts. */
export const ANOMALY_BAND_FILL = "rgba(239, 68, 68, 0.15)";

/** Translucent --accent (#16a34a) — optimal setpoint band overlay on charts. */
export const OPTIMAL_BAND_FILL = "rgba(22, 163, 74, 0.1)";
