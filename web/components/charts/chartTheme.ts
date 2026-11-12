/**
 * Design tokens for canvas charts.
 *
 * uPlot draws to a canvas, so it cannot consume the CSS custom properties in
 * globals.css — its axis labels fall back to the library default of solid
 * black, which is unreadable on the dark background. These helpers resolve the
 * tokens to concrete colors and hand uPlot *functions*, which it evaluates on
 * every draw: a theme flip then only needs a redraw, not a chart rebuild.
 */

import type uPlot from "uplot";

/** Alpha for the anomaly-time-span overlay painted behind the series. */
const ANOMALY_BAND_ALPHA = 0.12;

/** Used during SSR and in jsdom, where the stylesheet is not applied. */
const LIGHT_FALLBACK = {
  text: "#566864", // --text-muted
  line: "#dce5e2", // --border
  grid: "#e6ecea", // --chart-grid
  axis: "#566864", // --chart-axis
  chart1: "#1b7a6b", // --chart-1
  chart2: "#2563eb", // --chart-2
  chart3: "#d97706", // --chart-3
  chart4: "#db2777", // --chart-4
  chart5: "#7c3aed", // --chart-5
  chart6: "#65a30d", // --chart-6
  chart7: "#0891b2", // --chart-7
  chart8: "#475569", // --chart-8
  sevLstm: "#b5450a", // --sev-lstm
  sevCritical: "#c0262d", // --sev-critical
} as const;

interface ChartTokens {
  readonly text: string;
  readonly line: string;
  readonly grid: string;
  readonly axis: string;
  readonly chart1: string;
  readonly chart2: string;
  readonly chart3: string;
  readonly chart4: string;
  readonly chart5: string;
  readonly chart6: string;
  readonly chart7: string;
  readonly chart8: string;
  readonly sevLstm: string;
  readonly sevCritical: string;
  /** `sevCritical` at `ANOMALY_BAND_ALPHA` — concrete rgba(), canvas-safe. */
  readonly anomalyBand: string;
}

/** Resolved once per theme — getComputedStyle forces style recalc. */
let cached: ChartTokens | null = null;

function readToken(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value === "" ? fallback : value;
}

/**
 * Converts a `#rgb`/`#rrggbb` hex color to a concrete `rgba()` string.
 *
 * Canvas `fillStyle` does not reliably parse `color-mix()`, so alpha overlays
 * for theme colors (which are plain hex, not already translucent) are mixed
 * here in JS rather than via a CSS token.
 */
function hexToRgba(hex: string, alpha: number): string {
  const digits = hex.replace("#", "");
  const sixDigit = digits.length === 3 ? digits.replace(/./g, (c) => c + c) : digits;
  const red = Number.parseInt(sixDigit.slice(0, 2), 16);
  const green = Number.parseInt(sixDigit.slice(2, 4), 16);
  const blue = Number.parseInt(sixDigit.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

export function chartTokens(): ChartTokens {
  if (cached !== null) return cached;
  const sevCritical = readToken("--sev-critical", LIGHT_FALLBACK.sevCritical);
  cached = {
    text: readToken("--text-muted", LIGHT_FALLBACK.text),
    line: readToken("--border", LIGHT_FALLBACK.line),
    grid: readToken("--chart-grid", LIGHT_FALLBACK.grid),
    axis: readToken("--chart-axis", LIGHT_FALLBACK.axis),
    chart1: readToken("--chart-1", LIGHT_FALLBACK.chart1),
    chart2: readToken("--chart-2", LIGHT_FALLBACK.chart2),
    chart3: readToken("--chart-3", LIGHT_FALLBACK.chart3),
    chart4: readToken("--chart-4", LIGHT_FALLBACK.chart4),
    chart5: readToken("--chart-5", LIGHT_FALLBACK.chart5),
    chart6: readToken("--chart-6", LIGHT_FALLBACK.chart6),
    chart7: readToken("--chart-7", LIGHT_FALLBACK.chart7),
    chart8: readToken("--chart-8", LIGHT_FALLBACK.chart8),
    sevLstm: readToken("--sev-lstm", LIGHT_FALLBACK.sevLstm),
    sevCritical,
    anomalyBand: hexToRgba(sevCritical, ANOMALY_BAND_ALPHA),
  };
  return cached;
}

/** Drop the memo so the next draw re-reads the tokens. */
export function clearChartTokenCache(): void {
  cached = null;
}

/**
 * Axis colors as uPlot accepts them. Every field is a function so a redraw
 * after a theme change repaints in the new palette.
 */
export function themedAxisDefaults(): Pick<uPlot.Axis, "stroke" | "grid" | "ticks" | "border"> {
  return {
    stroke: () => chartTokens().axis,
    grid: { stroke: () => chartTokens().grid, width: 1 },
    ticks: { stroke: () => chartTokens().grid, width: 1 },
    border: { stroke: () => chartTokens().grid, width: 1 },
  };
}

/**
 * Call ``onChange`` whenever the active theme changes — both the explicit
 * ``data-theme`` attribute (ThemeToggle) and the OS preference, since
 * globals.css honours both. Returns an unsubscribe function.
 */
export function subscribeToThemeChange(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};

  const handle = (): void => {
    clearChartTokenCache();
    onChange();
  };

  const observer = new MutationObserver(handle);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  const media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", handle);

  return () => {
    observer.disconnect();
    media.removeEventListener("change", handle);
  };
}
