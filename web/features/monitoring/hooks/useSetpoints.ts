"use client";

import { useCallback, useEffect, useState } from "react";
import { z } from "zod";

import { CHANNEL_META } from "@/features/monitoring/lib/channelMeta";
import { useScope } from "@/lib/useScope";

export type SetpointRange = readonly [number, number];
export type Setpoints = Readonly<Record<string, SetpointRange>>;

// ---------------------------------------------------------------------------
// Zod schema — validate user-controlled localStorage data at runtime.
// ---------------------------------------------------------------------------

const setpointRangeSchema = z.tuple([z.number(), z.number()]);
const setpointsSchema = z.record(z.string(), setpointRangeSchema);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getDefaultSetpoints(): Setpoints {
  const defaults: Record<string, SetpointRange> = {};
  for (const [channel, meta] of Object.entries(CHANNEL_META)) {
    defaults[channel] = meta.optimal;
  }
  return defaults;
}

function buildStorageKey(tenant: string, gh: string): string {
  return `gh-setpoints:${tenant}:${gh}`;
}

/** SSR-safe localStorage read; returns empty object on any parse/access error. */
function loadOverrides(key: string): Partial<Setpoints> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return {};
    const parsed = JSON.parse(raw) as unknown;
    const result = setpointsSchema.safeParse(parsed);
    if (!result.success) return {};
    // Cast via unknown — Zod output type narrowed to readonly tuple above.
    return result.data as Partial<Setpoints>;
  } catch {
    return {};
  }
}

/** SSR-safe localStorage write; silently ignores errors (private-mode, quota). */
function persistOverrides(key: string, overrides: Partial<Setpoints>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(overrides));
  } catch {
    // Intentionally swallowed — UI still works without persistence.
  }
}

/** SSR-safe localStorage remove. */
function removeOverrides(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export interface UseSetpointsResult {
  /** Effective setpoints: CHANNEL_META defaults merged with user overrides. */
  readonly setpoints: Setpoints;
  /** Persist a new [min, max] for `channel`; no-op when min ≥ max. */
  readonly updateSetpoint: (channel: string, range: SetpointRange) => void;
  /** Clear all user overrides and revert to CHANNEL_META defaults. */
  readonly resetToDefaults: () => void;
}

/**
 * Manages per-tenant/greenhouse setpoint overrides, persisted to localStorage.
 *
 * Defaults come from `CHANNEL_META`. User overrides are merged on top and
 * stored under `gh-setpoints:{tenant}:{gh}`. Corrupted/invalid JSON is
 * silently discarded and falls back to defaults.
 */
export function useSetpoints(): UseSetpointsResult {
  const { tenant, gh } = useScope();
  const storageKey = buildStorageKey(tenant, gh);

  const [overrides, setOverrides] = useState<Partial<Setpoints>>(() => loadOverrides(storageKey));

  // Reload overrides when scope changes (tenant or gh switched).
  useEffect(() => {
    setOverrides(loadOverrides(storageKey));
  }, [storageKey]);

  const defaults = getDefaultSetpoints();
  // Spread is safe: defaults covers all keys, overrides values are non-undefined.
  // TypeScript can't infer this from Partial<Setpoints>, so we assert.
  const setpoints = { ...defaults, ...overrides } as Setpoints;

  const updateSetpoint = useCallback(
    (channel: string, range: SetpointRange): void => {
      const [min, max] = range;
      // Guard: min must be strictly less than max.
      if (min >= max) return;
      setOverrides((prev) => {
        const next = { ...prev, [channel]: range };
        persistOverrides(storageKey, next);
        return next;
      });
    },
    [storageKey],
  );

  const resetToDefaults = useCallback((): void => {
    setOverrides({});
    removeOverrides(storageKey);
  }, [storageKey]);

  return { setpoints, updateSetpoint, resetToDefaults };
}
