/**
 * uPlot paints to a canvas and cannot see CSS custom properties, so every
 * colour in the options object has to be a *function* it re-evaluates per
 * draw. A regression to `stroke: channelColor(i)` — the resolved string —
 * freezes the series at whatever theme was active when the memo built, and
 * passes every other gate: biome has no rule for it, `tsc` accepts both arms
 * of uPlot's value-or-accessor union, and `test/e2e/chart-theme.spec.ts`
 * samples only the x-axis strip, whose colours come from
 * `themedAxisDefaults()` and are function-valued by construction.
 *
 * `UPlotChart` is mocked to capture the options object rather than render a
 * canvas: jsdom has no 2d context, and the contract under test is the config,
 * not the paint.
 */

import { render } from "@testing-library/react";
import type uPlot from "uplot";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearChartTokenCache } from "@/components/charts/chartTheme";
import type { SensorRow } from "@/features/monitoring/api/readings";
import { SensorTimeSeriesChart } from "@/features/monitoring/components/SensorTimeSeriesChart";

type ChartOptions = Omit<uPlot.Options, "width" | "height">;

let captured: ChartOptions | null = null;

vi.mock("@/components/charts/UPlotChart", () => ({
  UPlotChart: ({ options }: { readonly options: ChartOptions }) => {
    captured = options;
    return null;
  },
}));

const ROWS: SensorRow[] = [
  { timestamp: "2026-06-01T00:00:00+00:00", temperature: 21, humidity: 60, anomaly_score: 0.9 },
  { timestamp: "2026-06-01T00:05:00+00:00", temperature: 22, humidity: 61, anomaly_score: 0.9 },
];
const CHANNELS = ["temperature", "humidity"];

/** Probe palettes, not the shipped ones: a fallback must not satisfy these. */
const FIRST_THEME: Record<string, string> = {
  "--chart-1": "#111111",
  "--chart-2": "#222222",
  "--sev-critical": "#333333",
};
const SECOND_THEME: Record<string, string> = {
  "--chart-1": "#aaaaaa",
  "--chart-2": "#bbbbbb",
  "--sev-critical": "#cccccc",
};

function setTokens(values: Record<string, string>): void {
  for (const [property, value] of Object.entries(values)) {
    document.documentElement.style.setProperty(property, value);
  }
  clearChartTokenCache();
}

/** The options object the component handed to uPlot, built under FIRST_THEME. */
function renderChart(): ChartOptions {
  setTokens(FIRST_THEME);
  render(
    <SensorTimeSeriesChart
      rows={ROWS}
      channels={CHANNELS}
      visible={new Set(CHANNELS)}
      threshold={0.5}
      scopeKey="test:acme:gh1"
    />,
  );
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

describe("SensorTimeSeriesChart series strokes", () => {
  it("hands uPlot a function per channel, never a resolved colour", () => {
    const series = renderChart().series;

    // series[0] is the x-axis and carries no stroke.
    expect(series.length).toBe(CHANNELS.length + 1);
    for (const [index, channel] of CHANNELS.entries()) {
      expect(typeof series[index + 1].stroke, channel).toBe("function");
    }
  });

  it("re-resolves each channel stroke after a theme flip", () => {
    const series = renderChart().series;

    // The options object is memoized at chart creation; the theme then flips,
    // and the already-built config must follow it.
    setTokens(SECOND_THEME);

    const strokes = CHANNELS.map((_, index) => series[index + 1].stroke as () => string);
    expect(strokes[0]()).toBe(SECOND_THEME["--chart-1"]);
    expect(strokes[1]()).toBe(SECOND_THEME["--chart-2"]);
  });

  it("repaints the anomaly band in the new theme on the next draw", () => {
    const options = renderChart();
    const drawClear = options.hooks?.drawClear?.[0] as (chart: uPlot) => void;
    expect(typeof drawClear).toBe("function");

    setTokens(SECOND_THEME);

    // Band geometry is computeAnomalyBands' contract (and has its own tests);
    // what matters here is that the fill is read per draw rather than captured
    // when the hook closure was built.
    const ctx = { fillStyle: "", save: () => {}, restore: () => {}, fillRect: () => {} };
    drawClear({
      ctx,
      bbox: { left: 0, top: 0, width: 100, height: 100 },
      valToPos: () => 0,
    } as unknown as uPlot);

    // #cccccc at the band alpha — concrete rgba, because canvas fillStyle does
    // not reliably parse color-mix().
    expect(ctx.fillStyle).toMatch(/^rgba\(204, 204, 204, 0?\.\d+\)$/);
  });
});
