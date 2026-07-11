"use client";

import { useMemo, useState } from "react";

import { Chip } from "@/components/ui/Chip";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";

type GroupBy = "date" | "severity";

interface GroupedAnomaliesProps {
  readonly anomalies: AnomalyRow[];
}

interface Group {
  readonly key: string;
  readonly rows: AnomalyRow[];
  readonly peak: number;
}

function groupAnomalies(anomalies: AnomalyRow[], groupBy: GroupBy): Group[] {
  const buckets = new Map<string, AnomalyRow[]>();
  for (const row of anomalies) {
    const key = groupBy === "date" ? row.timestamp.slice(0, 10) : row.severity;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(row);
    else buckets.set(key, [row]);
  }
  return [...buckets.entries()]
    .map(([key, rows]) => ({
      key,
      rows: rows.sort((a, b) => b.score - a.score),
      peak: Math.max(...rows.map((r) => r.score)),
    }))
    .sort((a, b) => b.peak - a.peak);
}

/** Grouped table view (plan §6.2 AnomalyGroupedTable) — grouping is pure
 *  client-side, so regrouping is instant. */
export function GroupedAnomalies({ anomalies }: GroupedAnomaliesProps) {
  const [groupBy, setGroupBy] = useState<GroupBy>("date");
  const groups = useMemo(() => groupAnomalies(anomalies, groupBy), [anomalies, groupBy]);

  if (anomalies.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-text-muted">
        No anomalies to group in this window.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm" data-testid="group-by">
        <span className="text-text-muted">Group by</span>
        {(["date", "severity"] as const).map((mode) => (
          <button
            key={mode}
            type="button"
            onClick={() => setGroupBy(mode)}
            aria-pressed={groupBy === mode}
            className={`rounded-[var(--radius-control)] border px-3 py-1 ${
              groupBy === mode
                ? "border-accent font-medium text-accent"
                : "border-border text-text-muted hover:text-text"
            }`}
          >
            {mode === "date" ? "Date" : "Severity"}
          </button>
        ))}
        <span className="ml-auto text-xs text-text-muted tabular-nums">
          {anomalies.length} anomalies · {groups.length} groups
        </span>
      </div>

      {groups.map((group) => (
        <details
          key={group.key}
          className="rounded-[var(--radius-card)] border border-border bg-bg-elevated"
          open={groups.length <= 3}
        >
          <summary className="cursor-pointer px-4 py-2.5 text-sm font-medium">
            {group.key} · {group.rows.length} record{group.rows.length === 1 ? "" : "s"} · peak{" "}
            <span className="tabular-nums">{group.peak.toFixed(3)}</span>
          </summary>
          <ul className="divide-y divide-border border-t border-border">
            {group.rows.slice(0, 20).map((row) => (
              <li
                key={row.timestamp}
                className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm"
              >
                <span className="tabular-nums">{row.timestamp.replace("T", " ").slice(0, 19)}</span>
                <Chip severity={row.severity}>{row.score.toFixed(3)}</Chip>
                <span className="ml-auto">
                  <MonitoringJumpLink timestamp={row.timestamp} />
                </span>
              </li>
            ))}
          </ul>
        </details>
      ))}
    </div>
  );
}
