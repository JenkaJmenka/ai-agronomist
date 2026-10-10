"use client";

import { useCallback, useEffect, useState } from "react";

import {
  DEFAULT_HOME_LAYOUT_PREFS,
  type HomeLayoutPrefs,
  loadHomeLayoutPrefs,
  saveHomeLayoutPrefs,
} from "@/features/app-configuration/lib/homeLayoutPrefs";

export function useHomeLayoutPrefs(): {
  readonly prefs: HomeLayoutPrefs;
  readonly setPrefs: (next: HomeLayoutPrefs) => void;
  readonly resetPrefs: () => void;
  readonly hydrated: boolean;
} {
  const [prefs, setPrefsState] = useState<HomeLayoutPrefs>(DEFAULT_HOME_LAYOUT_PREFS);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setPrefsState(loadHomeLayoutPrefs());
    setHydrated(true);
  }, []);

  const setPrefs = useCallback((next: HomeLayoutPrefs) => {
    setPrefsState(next);
    saveHomeLayoutPrefs(next);
  }, []);

  const resetPrefs = useCallback(() => {
    setPrefsState(DEFAULT_HOME_LAYOUT_PREFS);
    saveHomeLayoutPrefs(DEFAULT_HOME_LAYOUT_PREFS);
  }, []);

  return { prefs, setPrefs, resetPrefs, hydrated };
}
