/**
 * App Configuration prefs for Home layout (Phase B).
 * Persisted in localStorage; defaults match the Phase A fixed Home.
 */

import { HOME_DEFAULT_CHART_CHANNELS } from "@/features/home/lib/homeChannels";
import { DEFAULT_PERIOD_ID } from "@/lib/periodPresets";

export interface HomeLayoutPrefs {
  readonly showStatusBar: boolean;
  readonly showTiles: boolean;
  readonly showChart: boolean;
  readonly showAnomalyList: boolean;
  /** Preferred tile/chart channels (empty = climate-ordered discovery). */
  readonly primaryChannels: readonly string[];
  /** Period preset id applied when Home loads with no from/to in the URL. */
  readonly defaultPeriodId: string;
}

export const DEFAULT_HOME_LAYOUT_PREFS: HomeLayoutPrefs = {
  showStatusBar: true,
  showTiles: true,
  showChart: true,
  showAnomalyList: true,
  primaryChannels: [...HOME_DEFAULT_CHART_CHANNELS],
  defaultPeriodId: DEFAULT_PERIOD_ID,
};

const STORAGE_KEY = "aa-app-config:home-layout";

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function normalizePrefs(raw: unknown): HomeLayoutPrefs {
  if (raw === null || typeof raw !== "object") return DEFAULT_HOME_LAYOUT_PREFS;
  const obj = raw as Record<string, unknown>;
  return {
    showStatusBar:
      typeof obj.showStatusBar === "boolean"
        ? obj.showStatusBar
        : DEFAULT_HOME_LAYOUT_PREFS.showStatusBar,
    showTiles:
      typeof obj.showTiles === "boolean" ? obj.showTiles : DEFAULT_HOME_LAYOUT_PREFS.showTiles,
    showChart:
      typeof obj.showChart === "boolean" ? obj.showChart : DEFAULT_HOME_LAYOUT_PREFS.showChart,
    showAnomalyList:
      typeof obj.showAnomalyList === "boolean"
        ? obj.showAnomalyList
        : DEFAULT_HOME_LAYOUT_PREFS.showAnomalyList,
    primaryChannels: isStringArray(obj.primaryChannels)
      ? obj.primaryChannels
      : DEFAULT_HOME_LAYOUT_PREFS.primaryChannels,
    defaultPeriodId:
      typeof obj.defaultPeriodId === "string" && obj.defaultPeriodId.length > 0
        ? obj.defaultPeriodId
        : DEFAULT_HOME_LAYOUT_PREFS.defaultPeriodId,
  };
}

export function loadHomeLayoutPrefs(): HomeLayoutPrefs {
  if (typeof window === "undefined") return DEFAULT_HOME_LAYOUT_PREFS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_HOME_LAYOUT_PREFS;
    return normalizePrefs(JSON.parse(raw) as unknown);
  } catch {
    return DEFAULT_HOME_LAYOUT_PREFS;
  }
}

export function saveHomeLayoutPrefs(prefs: HomeLayoutPrefs): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
}
