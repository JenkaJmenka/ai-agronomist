"use client";

import { parseAsBoolean, parseAsFloat, useQueryState } from "nuqs";
import { useMemo } from "react";

import type { SensorRow } from "@/features/monitoring/api/readings";
import { useLiveReadings } from "@/features/monitoring/hooks/useLiveReadings";
import { useReadings } from "@/features/monitoring/hooks/useReadings";
import { rowsForActiveWindow } from "@/lib/periodPresets";
import { DEFAULT_SCORE_THRESHOLD, resolveRowSeverity, type Severity } from "@/lib/severity";

export interface AnomalyRow extends SensorRow {
  readonly score: number;
  readonly severity: Severity;
}

export interface AnomaliesState {
  readonly rows: SensorRow[];
  readonly anomalies: AnomalyRow[];
  readonly threshold: number;
  readonly setThreshold: (value: number) => void;
  readonly isLoading: boolean;
  /** A new window/scope is loading while stale rows stay on screen. */
  readonly isRefetching: boolean;
  readonly isError: boolean;
  readonly error: Error | null;
  readonly windowLabel: string;
  readonly downsampled: boolean;
  /** Pre-downsampling row count of the window (equals rows.length otherwise). */
  readonly originalCount: number;
}

/** Merge historical + live rows; live wins on duplicate timestamp. */
function dedupeByTimestamp(historical: SensorRow[], live: SensorRow[]): SensorRow[] {
  const map = new Map<string, SensorRow>();
  for (const row of historical) map.set(row.timestamp, row);
  for (const row of live) map.set(row.timestamp, row);
  return Array.from(map.values()).sort((a, b) =>
    a.timestamp < b.timestamp ? -1 : a.timestamp > b.timestamp ? 1 : 0,
  );
}

/**
 * One windowed fetch powers the score timeline and both tables: G1 keeps
 * anomaly rows in the downsampled payload, and flagging happens client-side
 * so threshold changes are instant (<500 ms budget, no refetch).
 *
 * When Live is on, SSE rows are merged in (same as Monitoring) so View from
 * the live anomaly strip is not empty while history has not caught up yet.
 */
export function useAnomalies(): AnomaliesState {
  const readings = useReadings();
  const [live] = useQueryState("live", parseAsBoolean.withDefault(false));
  const { rows: liveRows } = useLiveReadings(live);
  const [threshold, setThresholdRaw] = useQueryState(
    "threshold",
    parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD),
  );

  const rows = useMemo(() => {
    const merged = live ? dedupeByTimestamp(readings.rows, liveRows) : readings.rows;
    // While refetching, keep the previous payload unclipped — client-filtering
    // a downsampled wide window onto a narrow brush produces a sparse fake preview.
    return rowsForActiveWindow({
      rows: merged,
      from: readings.from,
      to: readings.to,
      isRefetching: readings.isRefetching,
    });
  }, [live, readings.rows, readings.from, readings.to, readings.isRefetching, liveRows]);

  const anomalies = useMemo<AnomalyRow[]>(() => {
    const flagged: AnomalyRow[] = [];
    for (const row of rows) {
      const score = row.anomaly_score;
      if (typeof score !== "number" || score < threshold) continue;
      // Chart rows come from the monitoring readings endpoint which does not
      // return detected_severity — always uses the score/threshold fallback.
      flagged.push({ ...row, score, severity: resolveRowSeverity({ score, threshold }) });
    }
    // Most severe first — mirrors the Streamlit Top Anomalies ordering.
    return flagged.sort((a, b) => b.score - a.score);
  }, [rows, threshold]);

  const hasLiveRows = live && liveRows.length > 0;

  return {
    rows,
    anomalies,
    threshold,
    setThreshold: (value: number) => void setThresholdRaw(value),
    isLoading: readings.isLoading && !hasLiveRows,
    isRefetching: readings.isRefetching,
    isError: readings.isError,
    error: readings.error,
    windowLabel: readings.windowLabel,
    downsampled: readings.downsampled,
    originalCount: Math.max(readings.originalCount, rows.length),
  };
}
