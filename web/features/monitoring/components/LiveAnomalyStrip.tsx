"use client";

import { useSearchParams } from "next/navigation";

import type { LiveAnomalyItem } from "@/features/monitoring/hooks/useLiveReadings";
import { pickDashboardSearchParams } from "@/lib/dashboardQuery";
import { formatTime } from "@/lib/formatTime";

interface LiveAnomalyStripProps {
  readonly anomalies: LiveAnomalyItem[];
  readonly threshold: number;
}

/** Pad around an anomaly timestamp when the event has no scoring window. */
const VIEW_PAD_MS = 30 * 60_000;

function anomalyViewWindow(a: LiveAnomalyItem): { from: string; to: string } | null {
  const startMs = Date.parse(a.window_start);
  const endMs = Date.parse(a.window_end);
  if (Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs) {
    return {
      from: new Date(startMs).toISOString(),
      to: new Date(endMs).toISOString(),
    };
  }
  const center = Date.parse(a.timestamp);
  if (!Number.isFinite(center)) return null;
  return {
    from: new Date(center - VIEW_PAD_MS).toISOString(),
    to: new Date(center + VIEW_PAD_MS).toISOString(),
  };
}

/**
 * Slim strip showing the latest live anomalies above the chart.
 * Each entry links to the Anomalies page focused on that episode's window,
 * with shared dashboard live/refresh/threshold/channel preserved.
 */
export function LiveAnomalyStrip({ anomalies, threshold }: LiveAnomalyStripProps) {
  const search = useSearchParams();

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
          const q = pickDashboardSearchParams(search);
          q.set("channel", a.affected_channels.join(","));
          q.set("threshold", String(threshold));
          // Focus Anomalies on the episode — the broad Live lookback often has
          // no persisted history yet, which showed as an empty page.
          const window = anomalyViewWindow(a);
          if (window) {
            q.set("from", window.from);
            q.set("to", window.to);
          }
          q.set("live", "true");
          const href = `/anomalies?${q.toString()}`;

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
