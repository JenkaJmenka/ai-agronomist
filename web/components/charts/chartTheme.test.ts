import { afterEach, describe, expect, it, vi } from "vitest";

import {
  chartTokens,
  clearChartTokenCache,
  subscribeToThemeChange,
  themedAxisDefaults,
} from "@/components/charts/chartTheme";

/** Applies custom properties to <html> the way globals.css does for a theme. */
function setTokens(values: Record<string, string>): void {
  for (const [property, value] of Object.entries(values)) {
    document.documentElement.style.setProperty(property, value);
  }
  clearChartTokenCache();
}

/**
 * Probe palettes — deliberately not the real token values. A test that set the
 * shipped colours would still pass if the module silently returned its
 * `LIGHT_FALLBACK` instead of reading CSS; these cannot.
 */
const LIGHT_PROBE: Record<string, string> = {
  "--text-muted": "#5d6b60",
  "--border": "#e2e8e2",
  "--chart-axis": "#4a5a52",
  "--chart-grid": "#eff3f1",
};

const DARK_PROBE: Record<string, string> = {
  "--text-muted": "#93a197",
  "--border": "#26302a",
  "--chart-axis": "#a1b4ad",
  "--chart-grid": "#26302a",
};

/** One distinct colour per series slot, so a crossed wire cannot pass. */
const SERIES_PROBE: Record<string, string> = Object.fromEntries(
  Array.from({ length: 8 }, (_, index) => [
    `--chart-${index + 1}`,
    `#1234${String(index + 1).padStart(2, "0")}`,
  ]),
);

/** Concrete hex, or the concrete rgba() the anomaly band is mixed down to. */
const CANVAS_SAFE_COLOR = /^(#[0-9a-f]{6}|rgba\(\d+, \d+, \d+, [\d.]+\))$/i;

afterEach(() => {
  // Clears every property any test set in one step — a test that introduces a
  // new token cannot leak into the next one by being forgotten here.
  document.documentElement.removeAttribute("style");
  document.documentElement.removeAttribute("data-theme");
  clearChartTokenCache();
});

describe("chartTokens", () => {
  it("resolves the CSS custom properties", () => {
    setTokens(DARK_PROBE);

    // toMatchObject rather than toEqual: the token set went from 2 keys to 15
    // in the Grovsen rebrand and will grow again, so an exact-shape assertion
    // costs an edit per addition and catches nothing in exchange — the
    // `ChartTokens` interface plus `tsc --noEmit` (a required gate) already pin
    // the key set, and a key that disappears fails its consumers at compile
    // time rather than here.
    expect(chartTokens()).toMatchObject({
      text: DARK_PROBE["--text-muted"],
      line: DARK_PROBE["--border"],
      axis: DARK_PROBE["--chart-axis"],
      grid: DARK_PROBE["--chart-grid"],
    });
  });

  it("resolves all eight series colours from their own tokens", () => {
    setTokens(SERIES_PROBE);

    const resolved: Record<string, string> = { ...chartTokens() };
    for (let slot = 1; slot <= 8; slot++) {
      expect(resolved[`chart${slot}`], `chart${slot}`).toBe(SERIES_PROBE[`--chart-${slot}`]);
    }
  });

  it("mixes the anomaly band down from the critical severity token", () => {
    setTokens({ "--sev-critical": "#c0262d" });

    const { sevCritical, anomalyBand } = chartTokens();
    expect(sevCritical).toBe("#c0262d");
    // Canvas fillStyle does not reliably parse color-mix(), so the overlay has
    // to arrive as a concrete translucent rgba() of that same colour.
    expect(anomalyBand).toMatch(/^rgba\(192, 38, 45, 0?\.\d+\)$/);
  });

  it("falls back to a concrete colour for every token when no stylesheet applies", () => {
    // SSR and jsdom: getComputedStyle returns "" for every custom property, and
    // "" reaches uPlot as its own default of solid black. The fallback *values*
    // are pinned against the light block of globals.css by the theme-token
    // guard (test/themeTokens.test.ts), so this asserts the mechanism instead
    // of copying the palette — the literal that stood here broke the moment the
    // design spec changed light --text-muted, for no coverage in return.
    clearChartTokenCache();

    const resolved: Record<string, string> = { ...chartTokens() };
    expect(Object.keys(resolved).length).toBeGreaterThan(0);
    for (const [key, value] of Object.entries(resolved)) {
      expect(value, key).toMatch(CANVAS_SAFE_COLOR);
      expect(value, key).not.toMatch(/^#0{3,6}$|^rgba\(0, 0, 0,/);
    }
  });

  it("re-reads after the cache is cleared, so a theme flip takes effect", () => {
    setTokens(LIGHT_PROBE);
    expect(chartTokens().text).toBe(LIGHT_PROBE["--text-muted"]);

    setTokens(DARK_PROBE);
    expect(chartTokens().text).toBe(DARK_PROBE["--text-muted"]);
  });
});

describe("themedAxisDefaults", () => {
  it("returns colors as functions so uPlot resolves them at draw time", () => {
    setTokens(LIGHT_PROBE);
    const axis = themedAxisDefaults();
    expect(typeof axis.stroke).toBe("function");

    // The axis config is built once at chart creation; the theme then flips.
    setTokens(DARK_PROBE);

    // Axis ink reads --chart-axis and all three line parts read --chart-grid.
    // The rebrand split those out of the --text-muted/--border pair this once
    // asserted, which is why the old expectation went stale.
    const stroke = axis.stroke as () => string;
    const gridStroke = axis.grid?.stroke as () => string;
    const ticksStroke = axis.ticks?.stroke as () => string;
    const borderStroke = axis.border?.stroke as () => string;
    expect(stroke()).toBe(DARK_PROBE["--chart-axis"]);
    expect(gridStroke()).toBe(DARK_PROBE["--chart-grid"]);
    expect(ticksStroke()).toBe(DARK_PROBE["--chart-grid"]);
    expect(borderStroke()).toBe(DARK_PROBE["--chart-grid"]);
  });

  it("never hands uPlot the black it defaults to", () => {
    setTokens(DARK_PROBE);
    const themed = themedAxisDefaults().stroke as () => string;
    expect(themed()).not.toBe("#000");
    expect(themed()).not.toBe("#000000");

    // The half that actually matters: with no custom property resolving at all
    // — SSR, jsdom, a stylesheet that failed to load — an empty token is the
    // one route by which uPlot's black can reach the canvas.
    document.documentElement.removeAttribute("style");
    clearChartTokenCache();
    const unstyled = themedAxisDefaults().stroke as () => string;
    expect(unstyled()).not.toBe("");
    expect(unstyled()).not.toBe("#000");
    expect(unstyled()).not.toBe("#000000");
  });
});

describe("subscribeToThemeChange", () => {
  it("fires when data-theme flips and stops after unsubscribe", async () => {
    const onChange = vi.fn();
    const unsubscribe = subscribeToThemeChange(onChange);

    document.documentElement.setAttribute("data-theme", "dark");
    await vi.waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));

    unsubscribe();
    document.documentElement.setAttribute("data-theme", "light");
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("invalidates the token cache before notifying", async () => {
    setTokens(LIGHT_PROBE);
    expect(chartTokens().text).toBe(LIGHT_PROBE["--text-muted"]);

    let observed: string | null = null;
    const unsubscribe = subscribeToThemeChange(() => {
      observed = chartTokens().text;
    });

    document.documentElement.style.setProperty("--text-muted", DARK_PROBE["--text-muted"]);
    document.documentElement.setAttribute("data-theme", "dark");
    await vi.waitFor(() => expect(observed).toBe(DARK_PROBE["--text-muted"]));

    unsubscribe();
  });
});
