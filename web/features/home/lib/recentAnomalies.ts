import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import type { LiveAnomalyItem } from "@/features/monitoring/hooks/useLiveReadings";
import { getSeverity, type Severity } from "@/lib/severity";

export interface HomeAnomalyItem {
  readonly id: string;
  readonly timestamp: string;
  readonly score: number;
  readonly severity: Severity;
  readonly sensors: readonly string[];
  readonly windowStart: string;
  readonly windowEnd: string;
}

const VIEW_PAD_MS = 30 * 60_000;

function padWindow(timestamp: string): { windowStart: string; windowEnd: string } {
  const center = Date.parse(timestamp);
  if (!Number.isFinite(center)) {
    return { windowStart: timestamp, windowEnd: timestamp };
  }
  return {
    windowStart: new Date(center - VIEW_PAD_MS).toISOString(),
    windowEnd: new Date(center + VIEW_PAD_MS).toISOString(),
  };
}

function fromLive(a: LiveAnomalyItem, threshold: number): HomeAnomalyItem {
  const startMs = Date.parse(a.window_start);
  const endMs = Date.parse(a.window_end);
  const hasWindow = Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs;
  const padded = padWindow(a.timestamp);
  return {
    id: a.seq || `live:${a.timestamp}:${a.score}`,
    timestamp: a.timestamp,
    score: a.score,
    severity: getSeverity({ score: a.score, threshold }),
    sensors: a.affected_channels,
    windowStart: hasWindow ? a.window_start : padded.windowStart,
    windowEnd: hasWindow ? a.window_end : padded.windowEnd,
  };
}

function sensorKeysFromRow(row: AnomalyRow): readonly string[] {
  const keys: string[] = [];
  for (const [key, value] of Object.entries(row)) {
    if (key === "timestamp" || key === "anomaly_score" || key === "score" || key === "severity") {
      continue;
    }
    if (typeof value === "number") keys.push(key);
  }
  return keys;
}

function fromFlagged(row: AnomalyRow): HomeAnomalyItem {
  const padded = padWindow(row.timestamp);
  return {
    id: `flagged:${row.timestamp}:${row.score}`,
    timestamp: row.timestamp,
    score: row.score,
    severity: row.severity,
    sensors: sensorKeysFromRow(row),
    windowStart: padded.windowStart,
    windowEnd: padded.windowEnd,
  };
}

/**
 * Prefer live anomalies when present; otherwise use flagged-window rows.
 * Newest / most severe first, capped.
 */
export function buildRecentAnomalies(options: {
  readonly live: boolean;
  readonly liveAnomalies: readonly LiveAnomalyItem[];
  readonly flagged: readonly AnomalyRow[];
  readonly threshold: number;
  readonly limit?: number;
}): HomeAnomalyItem[] {
  const limit = options.limit ?? 8;
  const source: HomeAnomalyItem[] =
    options.live && options.liveAnomalies.length > 0
      ? options.liveAnomalies.map((a) => fromLive(a, options.threshold))
      : options.flagged.map(fromFlagged);

  const seen = new Set<string>();
  const out: HomeAnomalyItem[] = [];
  for (const item of source) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    out.push(item);
    if (out.length >= limit) break;
  }
  return out;
}

export function countSeverities(items: readonly HomeAnomalyItem[]): {
  readonly critical: number;
  readonly warning: number;
  readonly lstm: number;
} {
  let critical = 0;
  let warning = 0;
  let lstm = 0;
  for (const item of items) {
    if (item.severity === "critical") critical += 1;
    else if (item.severity === "lstm") lstm += 1;
    else if (item.severity === "warning") warning += 1;
  }
  return { critical, warning, lstm };
}
