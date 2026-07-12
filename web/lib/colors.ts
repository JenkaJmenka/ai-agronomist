/** Okabe-Ito colorblind-safe categorical palette (plan §7.1). */
export const CHART_PALETTE = [
  "#0072b2", // blue
  "#e69f00", // orange
  "#009e73", // green
  "#cc79a7", // pink
  "#56b4e9", // sky
  "#d55e00", // vermillion
  "#f0e442", // yellow
  "#000000", // black
] as const;

export function channelColor(index: number): string {
  return CHART_PALETTE[index % CHART_PALETTE.length];
}

/** Translucent --sev-critical (#ef4444) — anomaly band overlay on charts. */
export const ANOMALY_BAND_FILL = "rgba(239, 68, 68, 0.15)";
