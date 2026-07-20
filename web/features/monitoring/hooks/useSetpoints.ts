"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { z } from "zod";

import { CHANNEL_META } from "@/features/monitoring/lib/channelMeta";
import { useScope } from "@/lib/useScope";

export type SetpointRange = readonly [number, number];
export type Setpoints = Readonly<Record<string, SetpointRange>>;

// ---------------------------------------------------------------------------
// Zod schema — validate user-controlled localStorage data at runtime.
// ---------------------------------------------------------------------------

const setpointRangeSchema = z
  .tuple([z.number(), z.number()])
  // F10: reject inverted ranges ([80, 20]) that would produce nonsense statuses.
  .refine(([min, max]) => min < max, "min must be less than max");

const setpointsSchema = z.record(z.string(), setpointRangeSchema);

// ---------------------------------------------------------------------------
// Module-scope defaults — CHANNEL_META is static; compute once on load.
// ---------------------------------------------------------------------------

// F9: hoisted to module scope so the hook never rebuilds defaults on every render.
const DEFAULT_SETPOINTS: Setpoints = Object.fromEntries(
  Object.entries(CHANNEL_META).map(([ch, meta]) => [ch, meta.optimal]),
) as Setpoints;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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
 *
 * F3: initializes to {} to avoid server/client hydration mismatch — localStorage
 * is unavailable on the server so a lazy useState initializer would differ between
 * SSR and hydration. The existing useEffect on [storageKey] loads overrides post-mount.
 */
export function useSetpoints(): UseSetpointsResult {
  const { tenant, gh } = useScope();
  const storageKey = buildStorageKey(tenant, gh);

  // F3: initialize to {} (not via lazy initializer) to avoid SSR/client hydration
  // mismatch. The useEffect below loads overrides after mount.
  const [overrides, setOverrides] = useState<Partial<Setpoints>>({});

  // Reload overrides when scope changes (tenant or gh switched), and on initial mount.
  useEffect(() => {
    setOverrides(loadOverrides(storageKey));
  }, [storageKey]);

  // F9: merge is memoized — only recomputes when overrides change (not every SSE tick).
  const setpoints = useMemo(
    () => ({ ...DEFAULT_SETPOINTS, ...overrides }) as Setpoints,
    [overrides],
  );

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
