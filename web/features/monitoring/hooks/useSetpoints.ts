"use client";

import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";

import { fetchOperationalConfig } from "@/features/configuration/api/operationalConfig";
import { CHANNEL_META } from "@/features/monitoring/lib/channelMeta";
import { STALE_TIME_MS } from "@/lib/query";
import { useScope } from "@/lib/useScope";

export type SetpointRange = readonly [number, number];
export type Setpoints = Readonly<Record<string, SetpointRange>>;

const setpointRangeSchema = z
  .tuple([z.number(), z.number()])
  .refine(([min, max]) => min < max, "min must be less than max");

const setpointsSchema = z.record(z.string(), setpointRangeSchema);

const DEFAULT_SETPOINTS: Setpoints = Object.fromEntries(
  Object.entries(CHANNEL_META).flatMap(([ch, meta]) =>
    meta.optimal !== undefined ? [[ch, meta.optimal]] : [],
  ),
) as Setpoints;

const MIGRATED_KEY_PREFIX = "gh-setpoints-migrated:";

function buildStorageKey(tenant: string, gh: string): string {
  return `gh-setpoints:${tenant}:${gh}`;
}

function buildMigratedKey(tenant: string, gh: string): string {
  return `${MIGRATED_KEY_PREFIX}${tenant}:${gh}`;
}

function loadOverrides(key: string): Partial<Setpoints> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return {};
    const parsed = JSON.parse(raw) as unknown;
    const result = setpointsSchema.safeParse(parsed);
    if (!result.success) return {};
    return result.data as Partial<Setpoints>;
  } catch {
    return {};
  }
}

function persistOverrides(key: string, overrides: Partial<Setpoints>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(overrides));
  } catch {
    // Intentionally swallowed — UI still works without persistence.
  }
}

function removeOverrides(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

/** Flatten operational day bands into Monitoring's [min,max] map. */
function dayBandsFromConfig(
  setpoints: Record<string, { day?: number[] | null; night?: number[] | null }> | undefined,
): Partial<Setpoints> {
  if (!setpoints) return {};
  const out: Record<string, SetpointRange> = {};
  for (const [channel, band] of Object.entries(setpoints)) {
    const day = band?.day;
    if (!Array.isArray(day) || day.length !== 2) continue;
    const min = Number(day[0]);
    const max = Number(day[1]);
    if (!Number.isFinite(min) || !Number.isFinite(max) || min >= max) continue;
    out[channel] = [min, max];
  }
  return out;
}

export interface UseSetpointsResult {
  /** Effective setpoints: CHANNEL_META ∪ server day bands ∪ local overrides. */
  readonly setpoints: Setpoints;
  /** Persist a new [min, max] for `channel`; no-op when min ≥ max. */
  readonly updateSetpoint: (channel: string, range: SetpointRange) => void;
  /** Clear all user overrides and revert to server/CHANNEL_META defaults. */
  readonly resetToDefaults: () => void;
}

/**
 * Setpoints with server hydrate + localStorage offline fallback.
 *
 * Priority (highest last): CHANNEL_META → operational-config day bands →
 * local overrides. Existing local overrides are kept as the offline cache;
 * a one-time migration flag marks that server config has been seen.
 */
export function useSetpoints(): UseSetpointsResult {
  const { tenant, gh } = useScope();
  const storageKey = buildStorageKey(tenant, gh);
  const migratedKey = buildMigratedKey(tenant, gh);
  const [overrides, setOverrides] = useState<Partial<Setpoints>>({});
  const migratedRef = useRef(false);

  const serverQuery = useQuery({
    queryKey: ["operational-config-setpoints", tenant, gh],
    queryFn: ({ signal }) => fetchOperationalConfig({ tenant, gh, signal }),
    staleTime: STALE_TIME_MS.windowedReadings,
    retry: 1,
  });

  useEffect(() => {
    setOverrides(loadOverrides(storageKey));
    migratedRef.current =
      typeof window !== "undefined" && window.localStorage.getItem(migratedKey) === "1";
  }, [storageKey, migratedKey]);

  // One-time: if local overrides exist and we have never marked migration,
  // keep them (offline wins) but stamp migration so later server edits can
  // refresh defaults underneath when overrides are cleared.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (migratedRef.current) return;
    if (!serverQuery.data) return;
    try {
      window.localStorage.setItem(migratedKey, "1");
      migratedRef.current = true;
    } catch {
      // ignore
    }
  }, [serverQuery.data, migratedKey]);

  const serverBands = useMemo(
    () => dayBandsFromConfig(serverQuery.data?.config.setpoints),
    [serverQuery.data],
  );

  const setpoints = useMemo(
    () => ({ ...DEFAULT_SETPOINTS, ...serverBands, ...overrides }) as Setpoints,
    [serverBands, overrides],
  );

  const updateSetpoint = useCallback(
    (channel: string, range: SetpointRange): void => {
      const [min, max] = range;
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
