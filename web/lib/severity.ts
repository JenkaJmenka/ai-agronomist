/** Severity semantics — single source of truth (MIGRATION_PLAN.md §7.2). */

export type Severity = "normal" | "warning" | "lstm" | "critical";

export const FAULT_LABELS: Readonly<Record<string, string>> = {
  spike_fault: "Spike fault",
  calibration_drift: "Calibration drift",
  stuck_fault: "Stuck sensor",
  bias_offset: "Bias offset",
  accuracy_decline: "Accuracy decline",
  unknown: "Unknown / unclassified",
};

interface SeverityInput {
  readonly score: number;
  readonly threshold: number;
  /** Detector family that produced the flag (e.g. "lstm", "statistical"). */
  readonly detector?: string;
  /** True when multiple detectors agree on this record. */
  readonly agreement?: boolean;
}

/**
 * Preserves the Streamlit dashboard's meaning: agreement → critical,
 * LSTM/drift window → lstm(orange), single-sensor statistical → warning,
 * below threshold → normal.
 */
export function getSeverity({ score, threshold, detector, agreement }: SeverityInput): Severity {
  if (score < threshold) return "normal";
  if (agreement) return "critical";
  if (detector?.includes("lstm") || detector?.includes("drift")) return "lstm";
  return "warning";
}
