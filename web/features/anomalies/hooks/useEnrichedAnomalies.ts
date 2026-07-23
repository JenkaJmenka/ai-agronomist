"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import {
  type EnrichedAnomaliesResult,
  fetchEnrichedAnomalies,
} from "@/features/anomalies/api/enriched";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import { STALE_TIME_MS } from "@/lib/query";
import { getSeverity } from "@/lib/severity";
import { useScope } from "@/lib/useScope";

/** Default cap when callers omit `limit` — Top Anomalies table size. */
const DEFAULT_ENRICHED_LIMIT = 50;

export interface AnomalyEnrichment {
  readonly faultType: string | null;
  readonly affectedSensors: readonly string[];
  readonly rootCause: string | null;
  /** Human-readable explanation naming the algorithm that flagged the row. */
  readonly reasoning: string | null;
}

export interface EnrichedAnomaliesState {
  /** Enrichment keyed by the row's ISO timestamp (rows carry no id). */
  readonly byTimestamp: ReadonlyMap<string, AnomalyEnrichment>;
  /** Server-ranked top anomalies for the window; null until loaded. The
   * Top Anomalies table must render these, not the downsampled chart rows:
   * chart rows can flag timestamps outside the server's top set, which
   * would render with no fault type (their enrichment lookup misses). */
  readonly topRows: readonly AnomalyRow[] | null;
  /** Full-window flagged count from the server; null until loaded. */
  readonly anomalyCount: number | null;
  readonly isLoading: boolean;
  /** True while a G4 request is in flight (incl. window changes after first load). */
  readonly isFetching: boolean;
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
  /** Cap for /enriched rows (default 50; raise for grouped table coverage). */
  limit?: number;
}): EnrichedAnomaliesState {
  const { tenant, gh } = useScope();
  const { windowStart, windowEnd, threshold, enabled } = params;
  const limit = params.limit ?? DEFAULT_ENRICHED_LIMIT;

  const query = useQuery<EnrichedAnomaliesResult, Error>({
    queryKey: ["anomalies-enriched", tenant, gh, windowStart, windowEnd, threshold, limit],
    queryFn: ({ signal }) =>
      fetchEnrichedAnomalies({
        tenant,
        gh,
        start: windowStart ?? "",
        end: windowEnd ?? "",
        threshold,
        limit,
        signal,
      }),
    enabled: enabled && windowStart !== null && windowEnd !== null,
    staleTime: STALE_TIME_MS.anomaliesInWindow,
    // Keep prior fault labels visible while a new window's enrich is deferred/in-flight.
    placeholderData: keepPreviousData,
    retry: 1,
  });

  const byTimestamp = useMemo(() => {
    const map = new Map<string, AnomalyEnrichment>();
    for (const row of query.data?.rows ?? []) {
      map.set(row.timestamp, {
        faultType: row.fault_type ?? null,
        affectedSensors: row.affected_sensors,
        rootCause: row.root_cause ?? null,
        reasoning: row.reasoning ?? null,
      });
    }
    return map;
  }, [query.data]);

  const topRows = useMemo<readonly AnomalyRow[] | null>(() => {
    if (!query.data) return null;
    const flagged: AnomalyRow[] = [];
    for (const row of query.data.rows) {
      const score = row.anomaly_score;
      if (typeof score !== "number") continue;
      flagged.push({
        timestamp: row.timestamp,
        anomaly_score: score,
        score,
        severity: getSeverity({ score, threshold }),
      });
    }
    return flagged.sort((a, b) => b.score - a.score);
  }, [query.data, threshold]);

  return {
    byTimestamp,
    topRows,
    anomalyCount: query.data?.anomalyCount ?? null,
    isLoading: query.isPending && query.isFetching,
    isFetching: query.isFetching,
  };
}
