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

/** Used during SSR and in jsdom, where the stylesheet is not applied. */
const LIGHT_FALLBACK = {
  text: "#5d6b60", // --text-muted
  line: "#e2e8e2", // --border
} as const;

interface ChartTokens {
  readonly text: string;
  readonly line: string;
}

/** Resolved once per theme — getComputedStyle forces style recalc. */
let cached: ChartTokens | null = null;

function readToken(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value === "" ? fallback : value;
}

export function chartTokens(): ChartTokens {
  if (cached !== null) return cached;
  cached = {
    text: readToken("--text-muted", LIGHT_FALLBACK.text),
    line: readToken("--border", LIGHT_FALLBACK.line),
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
    stroke: () => chartTokens().text,
    grid: { stroke: () => chartTokens().line, width: 1 },
    ticks: { stroke: () => chartTokens().line, width: 1 },
    border: { stroke: () => chartTokens().line, width: 1 },
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
