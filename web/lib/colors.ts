import { chartTokens } from "@/components/charts/chartTheme";

/**
 * Categorical series palette, 8 slots wide (plan §7.1) — themed via
 * `--chart-1`..`--chart-8` in globals.css rather than the fixed Okabe-Ito
 * hexes this used to hard-code. `CHART_COLOR_COUNT` lets a caller detect it
 * has wrapped past the palette (e.g. to switch a 9th+ series to a dashed
 * stroke) without hard-coding the width itself.
 */
export const CHART_COLOR_COUNT = 8;

export function channelColor(index: number): string {
  const tokens = chartTokens();
  const palette = [
    tokens.chart1,
    tokens.chart2,
    tokens.chart3,
    tokens.chart4,
    tokens.chart5,
    tokens.chart6,
    tokens.chart7,
    tokens.chart8,
  ];
  return palette[index % palette.length];
}
