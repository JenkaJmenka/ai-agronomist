import { afterEach, describe, expect, it, vi } from "vitest";

import {
  chartTokens,
  clearChartTokenCache,
  subscribeToThemeChange,
  themedAxisDefaults,
} from "@/components/charts/chartTheme";

function setTokens(text: string, line: string): void {
  document.documentElement.style.setProperty("--text-muted", text);
  document.documentElement.style.setProperty("--border", line);
  clearChartTokenCache();
}

afterEach(() => {
  document.documentElement.style.removeProperty("--text-muted");
  document.documentElement.style.removeProperty("--border");
  document.documentElement.removeAttribute("data-theme");
  clearChartTokenCache();
});

describe("chartTokens", () => {
  it("resolves the CSS custom properties", () => {
    setTokens("#93a197", "#26302a");
    expect(chartTokens()).toEqual({ text: "#93a197", line: "#26302a" });
  });

  it("falls back to the light palette when the token is unset", () => {
    clearChartTokenCache();
    expect(chartTokens().text).toBe("#5d6b60");
  });

  it("re-reads after the cache is cleared, so a theme flip takes effect", () => {
    setTokens("#5d6b60", "#e2e8e2");
    expect(chartTokens().text).toBe("#5d6b60");
    setTokens("#93a197", "#26302a");
    expect(chartTokens().text).toBe("#93a197");
  });
});

describe("themedAxisDefaults", () => {
  it("returns colors as functions so uPlot resolves them at draw time", () => {
    setTokens("#5d6b60", "#e2e8e2");
    const axis = themedAxisDefaults();
    expect(typeof axis.stroke).toBe("function");

    // The axis config is built once at chart creation; the theme then flips.
    setTokens("#93a197", "#26302a");

    const stroke = axis.stroke as () => string;
    const gridStroke = axis.grid?.stroke as () => string;
    expect(stroke()).toBe("#93a197");
    expect(gridStroke()).toBe("#26302a");
  });

  it("never hands uPlot the black it defaults to", () => {
    setTokens("#93a197", "#26302a");
    const stroke = themedAxisDefaults().stroke as () => string;
    expect(stroke()).not.toBe("#000");
    expect(stroke()).not.toBe("#000000");
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
    setTokens("#5d6b60", "#e2e8e2");
    expect(chartTokens().text).toBe("#5d6b60");

    let observed: string | null = null;
    const unsubscribe = subscribeToThemeChange(() => {
      observed = chartTokens().text;
    });

    document.documentElement.style.setProperty("--text-muted", "#93a197");
    document.documentElement.setAttribute("data-theme", "dark");
    await vi.waitFor(() => expect(observed).toBe("#93a197"));

    unsubscribe();
  });
});
