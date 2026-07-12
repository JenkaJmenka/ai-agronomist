"use client";

import { parseAsFloat, useQueryState } from "nuqs";
import { useMemo } from "react";

import type { SensorRow } from "@/features/monitoring/api/readings";
import { useReadings } from "@/features/monitoring/hooks/useReadings";
import { getSeverity, type Severity } from "@/lib/severity";

/** Matches the model's calibrated cutoff scale (scores top out ~0.2). */
const DEFAULT_THRESHOLD = 0.15;

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
  readonly isError: boolean;
  readonly error: Error | null;
  readonly windowLabel: string;
  readonly downsampled: boolean;
}

/**
 * One windowed fetch powers the score timeline and both tables: G1 keeps
 * anomaly rows in the downsampled payload, and flagging happens client-side
 * so threshold changes are instant (<500 ms budget, no refetch).
 */
export function useAnomalies(): AnomaliesState {
  const readings = useReadings();
  const [threshold, setThresholdRaw] = useQueryState(
    "threshold",
    parseAsFloat.withDefault(DEFAULT_THRESHOLD),
  );

  const anomalies = useMemo<AnomalyRow[]>(() => {
    const flagged: AnomalyRow[] = [];
    for (const row of readings.rows) {
      const score = row.anomaly_score;
      if (typeof score !== "number" || score < threshold) continue;
      flagged.push({ ...row, score, severity: getSeverity({ score, threshold }) });
    }
    // Most severe first — mirrors the Streamlit Top Anomalies ordering.
    return flagged.sort((a, b) => b.score - a.score);
  }, [readings.rows, threshold]);

  return {
    ...readings,
    anomalies,
    threshold,
    setThreshold: (value: number) => void setThresholdRaw(value),
  };
}
