/** Severity semantics — single source of truth (MIGRATION_PLAN.md §7.2). */

export type Severity = "unclassified" | "normal" | "warning" | "lstm" | "critical" | "urgent";

/** Matches the model's calibrated cutoff scale (scores top out ~0.2). */
export const DEFAULT_SCORE_THRESHOLD = 0.15;

/**
 * Numeric sort weight for severity comparisons.
 * Higher value = more severe; urgent (4) > critical (3) > lstm (2) > warning (1) > normal (0).
 *
 * "unclassified" sorts below "normal" (-1), not at "normal"'s rank: it means
 * the row was never run through severity classification (e.g. an LSTM-only
 * hit with no per-column attribution), not that it was assessed and found
 * benign. Ranking it with "normal" would repeat that conflation.
 */
export const SEVERITY_ORDER: Readonly<Record<Severity, number>> = {
  unclassified: -1,
  normal: 0,
  warning: 1,
  lstm: 2,
  critical: 3,
  urgent: 4,
};

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
}

/**
 * Fallback severity for rows without a server-assigned `detected_severity`
 * (rows predating migration 024). Can only return "normal" or "warning".
 *
 * The detector/agreement branches were dead code at all three call sites —
 * none passed those fields. They have been removed; use resolveRowSeverity
 * when the server-assigned severity should be preferred.
 */
export function getSeverity({ score, threshold }: SeverityInput): Severity {
  if (score < threshold) return "normal";
  return "warning";
}

/**
 * Values the server may send as `detected_severity`. "unclassified" is the
 * server's explicit sentinel for "never run through severity classification"
 * (episode_grouper.py::_severity_rank) — distinct from a missing/null field,
 * which still falls back to score/threshold below.
 */
const VALID_SEVERITIES = new Set<string>([
  "unclassified",
  "normal",
  "warning",
  "lstm",
  "critical",
  "urgent",
]);

/**
 * Prefer the server's `detected_severity` over client re-derivation.
 * Falls back to score/threshold for rows where the server sends nothing
 * (rows predating migration 024, or the LSTM-only path with no per-column
 * attribution).
 */
export function resolveRowSeverity({
  score,
  threshold,
  detectedSeverity,
}: {
  readonly score: number;
  readonly threshold: number;
  readonly detectedSeverity?: string | null;
}): Severity {
  if (detectedSeverity != null && VALID_SEVERITIES.has(detectedSeverity)) {
    return detectedSeverity as Severity;
  }
  return getSeverity({ score, threshold });
}
