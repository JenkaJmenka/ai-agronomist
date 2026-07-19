"use client";

import type { LiveAnomalyItem } from "@/features/monitoring/hooks/useLiveReadings";
import { formatTime } from "@/lib/formatTime";

interface LiveAnomalyStripProps {
  readonly anomalies: LiveAnomalyItem[];
  readonly threshold: number;
}

/**
 * Slim strip showing the latest live anomalies above the chart.
 * Each entry links to the Anomalies page with the affected channels and
 * threshold pre-selected (mirrors the existing deep-link pattern).
 * Upgraded in place by `anomaly-detail` SSE events via the parent.
 */
export function LiveAnomalyStrip({ anomalies, threshold }: LiveAnomalyStripProps) {
  // Safety-net dedupe: the hook upserts re-emits by seq, but as a display-layer
  // guard we also collapse any remaining duplicates (e.g. empty-seq items that
  // can't be matched). For non-empty seqs the first occurrence (newest, since
  // anomalies are newest-first) is kept; empty-seq items are always kept.
  const seenSeqs = new Set<string>();
  const displayAnomalies = anomalies.filter((a) => {
    if (!a.seq) return true;
    if (seenSeqs.has(a.seq)) return false;
    seenSeqs.add(a.seq);
    return true;
  });

  if (displayAnomalies.length === 0) {
    return null;
  }

  return (
    <div
      className="flex flex-col gap-1 rounded-md border border-amber-500/30 bg-amber-500/5 p-2"
      data-testid="live-anomaly-strip"
    >
      <p className="text-xs font-semibold text-amber-600">Live anomalies</p>
      <ul className="flex flex-col gap-0.5" data-testid="live-anomaly-list">
        {displayAnomalies.map((a) => {
          const time = formatTime(a.timestamp);
          const channelParam = encodeURIComponent(a.affected_channels.join(","));
          const href = `/anomalies?channel=${channelParam}&threshold=${threshold}`;

          return (
            <li
              key={a.seq || a.timestamp}
              className="flex items-center gap-2 text-xs"
              data-testid={`live-anomaly-item-${a.seq || a.timestamp}`}
            >
              <span className="tabular-nums text-text-muted">{time}</span>
              <span className="text-amber-600">{a.affected_channels.join(", ")}</span>
              <span className="tabular-nums text-text-muted">score {a.score.toFixed(2)}</span>
              {a.fault_type && (
                <span className="text-text-muted">{a.fault_type.replace(/_/g, " ")}</span>
              )}
              <a
                href={href}
                className="ml-auto text-accent underline-offset-2 hover:underline"
                data-testid={`live-anomaly-link-${a.seq || a.timestamp}`}
              >
                View
              </a>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
