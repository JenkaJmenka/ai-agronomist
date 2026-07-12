"use client";

import { useMemo, useState } from "react";

import { Chip } from "@/components/ui/Chip";
import { Skeleton } from "@/components/ui/Skeleton";
import type { ServerGroupBy } from "@/features/anomalies/api/enriched";
import { SERVER_GROUPING_THRESHOLD } from "@/features/anomalies/api/flaggedWindow";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import { useAnomalyGroups } from "@/features/anomalies/hooks/useAnomalyGroups";
import { useFlaggedWindow } from "@/features/anomalies/hooks/useFlaggedWindow";
import { FAULT_LABELS } from "@/lib/severity";

const CLIENT_GROUP_ROW_PREVIEW = 20;
const SERVER_GROUP_LABELS: Readonly<Record<ServerGroupBy, string>> = {
  date: "Date",
  type: "Fault type",
  sensor: "Sensor",
};

type ClientGroupBy = "date" | "severity";

interface GroupedAnomaliesProps {
  /** Full-window flagged total (server-side) — decides client vs G3 grouping. */
  readonly anomalyCount: number;
  readonly windowStart: string | null;
  readonly windowEnd: string | null;
  readonly threshold: number;
}

interface ClientGroup {
  readonly key: string;
  readonly rows: AnomalyRow[];
  readonly peak: number;
}

function groupClientSide(anomalies: AnomalyRow[], groupBy: ClientGroupBy): ClientGroup[] {
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

function GroupModeButton(props: {
  readonly label: string;
  readonly isActive: boolean;
  readonly onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={props.onClick}
      aria-pressed={props.isActive}
      className={`rounded-[var(--radius-control)] border px-3 py-1 ${
        props.isActive
          ? "border-accent font-medium text-accent"
          : "border-border text-text-muted hover:text-text"
      }`}
    >
      {props.label}
    </button>
  );
}

/**
 * Grouped table view (plan §6.2 AnomalyGroupedTable). Under
 * SERVER_GROUPING_THRESHOLD flagged rows grouping is client compute over the
 * exact G2 flagged rows (never the downsampled chart rows, which drop
 * anomalies unevenly per range); above it the G3 endpoint aggregates
 * server-side so huge windows never ship every row.
 */
export function GroupedAnomalies({
  anomalyCount,
  windowStart,
  windowEnd,
  threshold,
}: GroupedAnomaliesProps) {
  const [clientGroupBy, setClientGroupBy] = useState<ClientGroupBy>("date");
  const [serverGroupBy, setServerGroupBy] = useState<ServerGroupBy>("date");
  const useServer = anomalyCount >= SERVER_GROUPING_THRESHOLD;

  const serverState = useAnomalyGroups({
    windowStart,
    windowEnd,
    threshold,
    groupBy: serverGroupBy,
    enabled: useServer,
  });
  const flagged = useFlaggedWindow({
    windowStart,
    windowEnd,
    threshold,
    enabled: !useServer,
  });
  const clientGroups = useMemo(
    () => (useServer ? [] : groupClientSide(flagged.anomalies, clientGroupBy)),
    [flagged.anomalies, clientGroupBy, useServer],
  );

  if (!useServer && flagged.isLoading) {
    return <Skeleton className="h-40 w-full" />;
  }

  if (!useServer && flagged.isError) {
    return (
      <p role="alert" className="py-4 text-center text-sm text-sev-critical">
        Could not load the window’s anomalies — narrow the window or retry.
      </p>
    );
  }

  if (!useServer && flagged.anomalies.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-text-muted">
        No anomalies to group in this window.
      </p>
    );
  }

  if (useServer) {
    return (
      <div className="space-y-3" data-testid="server-grouped">
        <div className="flex items-center gap-2 text-sm" data-testid="group-by">
          <span className="text-text-muted">Group by</span>
          {(Object.keys(SERVER_GROUP_LABELS) as ServerGroupBy[]).map((mode) => (
            <GroupModeButton
              key={mode}
              label={SERVER_GROUP_LABELS[mode]}
              isActive={serverGroupBy === mode}
              onClick={() => setServerGroupBy(mode)}
            />
          ))}
          <span className="ml-auto text-xs text-text-muted tabular-nums">
            {anomalyCount.toLocaleString()} anomalies · grouped server-side
          </span>
        </div>

        {serverState.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : serverState.isError ? (
          <p role="alert" className="py-4 text-center text-sm text-sev-critical">
            Could not load server groups — narrow the window or retry.
          </p>
        ) : (
          serverState.groups.map((group) => (
            <details
              key={group.key}
              className="rounded-[var(--radius-card)] border border-border bg-bg-elevated"
            >
              <summary className="cursor-pointer px-4 py-2.5 text-sm font-medium">
                {serverGroupBy === "type" ? (FAULT_LABELS[group.key] ?? group.key) : group.key} ·{" "}
                {group.count.toLocaleString()} record{group.count === 1 ? "" : "s"} · peak{" "}
                <span className="tabular-nums">{group.top_score.toFixed(3)}</span>
              </summary>
              <ul className="divide-y divide-border border-t border-border">
                {group.sample_ids.map((sampleTimestamp) => (
                  <li
                    key={sampleTimestamp}
                    className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm"
                  >
                    <span className="tabular-nums">
                      {sampleTimestamp.replace("T", " ").slice(0, 19)}
                    </span>
                    <span className="ml-auto">
                      <MonitoringJumpLink
                        timestamp={sampleTimestamp}
                        channels={serverGroupBy === "sensor" ? [group.key] : null}
                      />
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          ))
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm" data-testid="group-by">
        <span className="text-text-muted">Group by</span>
        {(["date", "severity"] as const).map((mode) => (
          <GroupModeButton
            key={mode}
            label={mode === "date" ? "Date" : "Severity"}
            isActive={clientGroupBy === mode}
            onClick={() => setClientGroupBy(mode)}
          />
        ))}
        <span className="ml-auto text-xs text-text-muted tabular-nums">
          {flagged.anomalies.length} anomalies · {clientGroups.length} groups
        </span>
      </div>

      {clientGroups.map((group) => (
        <details
          key={group.key}
          className="rounded-[var(--radius-card)] border border-border bg-bg-elevated"
          open={clientGroups.length <= 3}
        >
          <summary className="cursor-pointer px-4 py-2.5 text-sm font-medium">
            {group.key} · {group.rows.length} record{group.rows.length === 1 ? "" : "s"} · peak{" "}
            <span className="tabular-nums">{group.peak.toFixed(3)}</span>
          </summary>
          <ul className="divide-y divide-border border-t border-border">
            {group.rows.slice(0, CLIENT_GROUP_ROW_PREVIEW).map((row) => (
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
