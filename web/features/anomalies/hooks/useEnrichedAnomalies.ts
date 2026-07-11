"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { type EnrichedAnomalyRow, fetchEnrichedAnomalies } from "@/features/anomalies/api/enriched";
import { STALE_TIME_MS } from "@/lib/query";
import { useScope } from "@/lib/useScope";

/** Matches the Top Anomalies table cap in AnomalyTable. */
const ENRICHED_LIMIT = 50;

export interface AnomalyEnrichment {
  readonly faultType: string | null;
  readonly affectedSensors: readonly string[];
  readonly rootCause: string | null;
}

export interface EnrichedAnomaliesState {
  /** Enrichment keyed by the row's ISO timestamp (rows carry no id). */
  readonly byTimestamp: ReadonlyMap<string, AnomalyEnrichment>;
  readonly isLoading: boolean;
}

/**
 * Fault-type / root-cause enrichment for the current anomaly window (G4).
 * Window bounds come from the already-fetched envelope so no extra bounds
 * round-trip is needed; enrichment failing never blocks the tables — they
 * just render without fault labels.
 */
export function useEnrichedAnomalies(params: {
  windowStart: string | null;
  windowEnd: string | null;
  threshold: number;
  enabled: boolean;
}): EnrichedAnomaliesState {
  const { tenant, gh } = useScope();
  const { windowStart, windowEnd, threshold, enabled } = params;

  const query = useQuery<EnrichedAnomalyRow[], Error>({
    queryKey: ["anomalies-enriched", tenant, gh, windowStart, windowEnd, threshold],
    queryFn: () =>
      fetchEnrichedAnomalies({
        tenant,
        gh,
        start: windowStart ?? "",
        end: windowEnd ?? "",
        threshold,
        limit: ENRICHED_LIMIT,
      }),
    enabled: enabled && windowStart !== null && windowEnd !== null,
    staleTime: STALE_TIME_MS.anomaliesInWindow,
    retry: 1,
  });

  const byTimestamp = useMemo(() => {
    const map = new Map<string, AnomalyEnrichment>();
    for (const row of query.data ?? []) {
      map.set(row.timestamp, {
        faultType: row.fault_type ?? null,
        affectedSensors: row.affected_sensors,
        rootCause: row.root_cause ?? null,
      });
    }
    return map;
  }, [query.data]);

  return { byTimestamp, isLoading: query.isPending && query.isFetching };
}
