/**
 * The score and threshold series must hand uPlot *functions*, not resolved
 * colour strings — see the note in
 * `features/monitoring/components/SensorTimeSeriesChart.test.tsx` for why
 * every other gate accepts the frozen-colour regression.
 *
 * The capture mock is repeated rather than shared because `vi.mock` is hoisted
 * per file and cannot be imported from a helper.
 */

import { render } from "@testing-library/react";
import type uPlot from "uplot";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearChartTokenCache } from "@/components/charts/chartTheme";
import { ScoreTimeline } from "@/features/anomalies/components/ScoreTimeline";
import type { SensorRow } from "@/features/monitoring/api/readings";

type ChartOptions = Omit<uPlot.Options, "width" | "height">;

let captured: ChartOptions | null = null;

vi.mock("@/components/charts/UPlotChart", () => ({
  UPlotChart: ({ options }: { readonly options: ChartOptions }) => {
    captured = options;
    return null;
  },
}));

const ROWS: SensorRow[] = [
  { timestamp: "2026-06-01T00:00:00+00:00", anomaly_score: 0.2 },
  { timestamp: "2026-06-01T00:05:00+00:00", anomaly_score: 0.8 },
];

/** Probe colours, not the shipped ones: a fallback must not satisfy these. */
const FIRST_THEME: Record<string, string> = {
  "--sev-lstm": "#111111",
  "--sev-critical": "#222222",
};
const SECOND_THEME: Record<string, string> = {
  "--sev-lstm": "#aaaaaa",
  "--sev-critical": "#bbbbbb",
};

function setTokens(values: Record<string, string>): void {
  for (const [property, value] of Object.entries(values)) {
    document.documentElement.style.setProperty(property, value);
  }
  clearChartTokenCache();
}

function renderTimeline(): ChartOptions {
  setTokens(FIRST_THEME);
  render(<ScoreTimeline rows={ROWS} threshold={0.5} scopeKey="anomalies:acme:gh1" />);
  if (captured === null) throw new Error("UPlotChart was never rendered");
  return captured;
}

beforeEach(() => {
  captured = null;
});

afterEach(() => {
  document.documentElement.removeAttribute("style");
  clearChartTokenCache();
});

describe("ScoreTimeline series strokes", () => {
  it("hands uPlot a function for both the score and the threshold line", () => {
    const series = renderTimeline().series;

    // series[0] is the x-axis; [1] is the score, [2] the threshold reference.
    expect(series.length).toBe(3);
    expect(typeof series[1].stroke, "anomaly score").toBe("function");
    expect(typeof series[2].stroke, "threshold").toBe("function");
  });

  it("re-resolves both strokes after a theme flip", () => {
    const series = renderTimeline().series;

    // The options object is memoized at chart creation; the theme then flips.
    setTokens(SECOND_THEME);

    const score = series[1].stroke as () => string;
    const threshold = series[2].stroke as () => string;
    expect(score()).toBe(SECOND_THEME["--sev-lstm"]);
    expect(threshold()).toBe(SECOND_THEME["--sev-critical"]);
  });
});
