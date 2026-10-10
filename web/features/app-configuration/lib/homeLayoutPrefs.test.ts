import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  DEFAULT_HOME_LAYOUT_PREFS,
  loadHomeLayoutPrefs,
  saveHomeLayoutPrefs,
} from "@/features/app-configuration/lib/homeLayoutPrefs";

describe("homeLayoutPrefs", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("returns defaults when empty", () => {
    expect(loadHomeLayoutPrefs()).toEqual(DEFAULT_HOME_LAYOUT_PREFS);
  });

  it("round-trips saved prefs", () => {
    const next = {
      ...DEFAULT_HOME_LAYOUT_PREFS,
      showChart: false,
      primaryChannels: ["humidity", "co2"],
      defaultPeriodId: "1h",
    };
    saveHomeLayoutPrefs(next);
    expect(loadHomeLayoutPrefs()).toEqual(next);
  });

  it("falls back on corrupt JSON", () => {
    window.localStorage.setItem("aa-app-config:home-layout", "{not-json");
    expect(loadHomeLayoutPrefs()).toEqual(DEFAULT_HOME_LAYOUT_PREFS);
  });

  it("ignores invalid field types", () => {
    window.localStorage.setItem(
      "aa-app-config:home-layout",
      JSON.stringify({ showTiles: "yes", primaryChannels: "temperature" }),
    );
    const prefs = loadHomeLayoutPrefs();
    expect(prefs.showTiles).toBe(DEFAULT_HOME_LAYOUT_PREFS.showTiles);
    expect(prefs.primaryChannels).toEqual(DEFAULT_HOME_LAYOUT_PREFS.primaryChannels);
  });
});
