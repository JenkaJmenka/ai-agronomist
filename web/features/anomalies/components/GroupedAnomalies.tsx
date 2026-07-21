"use client";

import { useMemo, useState } from "react";

import { Chip } from "@/components/ui/Chip";
import { ExportCsvButton } from "@/components/ui/ExportCsvButton";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import type { AnomalyGroupSample, ServerGroupBy } from "@/features/anomalies/api/enriched";
import { SERVER_GROUPING_THRESHOLD } from "@/features/anomalies/api/flaggedWindow";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import { useAnomalyGroups } from "@/features/anomalies/hooks/useAnomalyGroups";
import {
  type AnomalyEnrichment,
  useEnrichedAnomalies,
} from "@/features/anomalies/hooks/useEnrichedAnomalies";
import { useFlaggedWindow } from "@/features/anomalies/hooks/useFlaggedWindow";
import {
  formatSensorList,
  formatSensorValues,
} from "@/features/anomalies/lib/formatAnomalyDetails";
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

function AnomalyDetailRow(props: {
  readonly timestamp: string;
  readonly score: number | null;
  readonly severity?: AnomalyRow["severity"];
  readonly sensors: readonly string[];
  readonly values: Readonly<Record<string, unknown>>;
  readonly reasoning: string | null;
  readonly jumpChannels: readonly string[] | null;
}) {
  const sensorLabel = formatSensorList(props.sensors);
  const valueLabel = formatSensorValues(props.values, props.sensors);
  return (
    <li className="flex flex-col gap-1 px-4 py-2 text-sm" data-testid="grouped-anomaly-row">
      <div className="flex flex-wrap items-center gap-3">
        <span className="tabular-nums">{props.timestamp.replace("T", " ").slice(0, 19)}</span>
        {props.score !== null && props.severity ? (
          <Chip severity={props.severity}>{props.score.toFixed(3)}</Chip>
        ) : props.score !== null ? (
          <span className="tabular-nums text-text-muted">{props.score.toFixed(3)}</span>
        ) : null}
        <span className="ml-auto">
          <MonitoringJumpLink
            timestamp={props.timestamp}
            channels={
              props.jumpChannels && props.jumpChannels.length > 0 ? props.jumpChannels : null
            }
          />
        </span>
      </div>
      <div className="grid gap-0.5 text-xs text-text-muted sm:grid-cols-[7rem_1fr]">
        <span className="font-medium text-text">Sensors</span>
        <span data-testid="grouped-anomaly-sensors">{sensorLabel}</span>
        <span className="font-medium text-text">Values</span>
        <span className="tabular-nums" data-testid="grouped-anomaly-values">
          {valueLabel || "—"}
        </span>
        <span className="font-medium text-text">Reasoning</span>
        <span data-testid="grouped-anomaly-reasoning">{props.reasoning?.trim() || "—"}</span>
      </div>
    </li>
  );
}

function enrichmentFor(
  map: ReadonlyMap<string, AnomalyEnrichment>,
  timestamp: string,
): AnomalyEnrichment | undefined {
  return map.get(timestamp);
}

function clientRowValues(row: AnomalyRow): Record<string, unknown> {
  return row as Record<string, unknown>;
}

/**
 * Grouped table view (plan §6.2 AnomalyGroupedTable). Under
 * SERVER_GROUPING_THRESHOLD flagged rows grouping is client compute over the
 * exact G2 flagged rows; above it the G3 endpoint aggregates server-side.
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
  // Enrichment for client groups — same window, up to the client-side cap.
  const enrichment = useEnrichedAnomalies({
    windowStart,
    windowEnd,
    threshold,
    enabled: !useServer && flagged.anomalies.length > 0,
    limit: SERVER_GROUPING_THRESHOLD,
  });
  const clientGroups = useMemo(
    () => (useServer ? [] : groupClientSide(flagged.anomalies, clientGroupBy)),
    [flagged.anomalies, clientGroupBy, useServer],
  );

  const exportRows = useMemo(() => {
    if (useServer) {
      const rows: unknown[][] = [];
      for (const group of serverState.groups) {
        const samples: AnomalyGroupSample[] =
          group.samples.length > 0
            ? group.samples
            : group.sample_ids.map((timestamp) => ({
                timestamp,
                anomaly_score: null,
                affected_sensors: [],
                reasoning: null,
                values: {},
              }));
        for (const sample of samples) {
          rows.push([
            group.key,
            sample.timestamp,
            sample.anomaly_score ?? "",
            formatSensorList(sample.affected_sensors),
            formatSensorValues(sample.values, sample.affected_sensors),
            sample.reasoning ?? "",
          ]);
        }
      }
      return rows;
    }
    const rows: unknown[][] = [];
    for (const group of clientGroups) {
      for (const row of group.rows) {
        const detail = enrichmentFor(enrichment.byTimestamp, row.timestamp);
        const sensors = detail?.affectedSensors ?? [];
        rows.push([
          group.key,
          row.timestamp,
          row.score,
          formatSensorList(sensors),
          formatSensorValues(clientRowValues(row), sensors),
          detail?.reasoning ?? "",
        ]);
      }
    }
    return rows;
  }, [useServer, serverState.groups, clientGroups, enrichment.byTimestamp]);

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

  const exportHeaders = ["group", "timestamp", "score", "sensors", "values", "reasoning"] as const;

  if (useServer) {
    return (
      <div className="space-y-3" data-testid="server-grouped">
        <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="group-by">
          <span className="text-text-muted">Group by</span>
          {(Object.keys(SERVER_GROUP_LABELS) as ServerGroupBy[]).map((mode) => (
            <GroupModeButton
              key={mode}
              label={SERVER_GROUP_LABELS[mode]}
              isActive={serverGroupBy === mode}
              onClick={() => setServerGroupBy(mode)}
            />
          ))}
          <span className="ml-auto flex items-center gap-2 text-xs text-text-muted tabular-nums">
            {anomalyCount.toLocaleString()} anomalies · grouped server-side
            <ExportCsvButton
              filename="anomalies-grouped"
              headers={exportHeaders}
              rows={exportRows}
              testId="export-grouped-csv"
            />
          </span>
        </div>

        {serverState.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : serverState.isError ? (
          <p role="alert" className="py-4 text-center text-sm text-sev-critical">
            Could not load server groups — narrow the window or retry.
          </p>
        ) : (
          <RefetchingBlock isRefetching={serverState.isRefetching} className="space-y-3">
            {serverState.groups.map((group) => {
              const samples: AnomalyGroupSample[] =
                group.samples.length > 0
                  ? group.samples
                  : group.sample_ids.map((timestamp) => ({
                      timestamp,
                      anomaly_score: null,
                      affected_sensors: [],
                      reasoning: null,
                      values: {},
                    }));
              return (
                <details
                  key={group.key}
                  className="rounded-[var(--radius-card)] border border-border bg-bg-elevated"
                >
                  <summary className="cursor-pointer px-4 py-2.5 text-sm font-medium">
                    {serverGroupBy === "type" ? (FAULT_LABELS[group.key] ?? group.key) : group.key}{" "}
                    · {group.count.toLocaleString()} record{group.count === 1 ? "" : "s"} · peak{" "}
                    <span className="tabular-nums">{group.top_score.toFixed(3)}</span>
                  </summary>
                  <ul className="divide-y divide-border border-t border-border">
                    {samples.map((sample) => (
                      <AnomalyDetailRow
                        key={sample.timestamp}
                        timestamp={sample.timestamp}
                        score={sample.anomaly_score ?? null}
                        sensors={
                          serverGroupBy === "sensor" && sample.affected_sensors.length === 0
                            ? [group.key]
                            : sample.affected_sensors
                        }
                        values={sample.values}
                        reasoning={sample.reasoning ?? null}
                        jumpChannels={
                          serverGroupBy === "sensor" ? [group.key] : sample.affected_sensors
                        }
                      />
                    ))}
                  </ul>
                </details>
              );
            })}
          </RefetchingBlock>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="group-by">
        <span className="text-text-muted">Group by</span>
        {(["date", "severity"] as const).map((mode) => (
          <GroupModeButton
            key={mode}
            label={mode === "date" ? "Date" : "Severity"}
            isActive={clientGroupBy === mode}
            onClick={() => setClientGroupBy(mode)}
          />
        ))}
        <span className="ml-auto flex items-center gap-2 text-xs text-text-muted tabular-nums">
          {flagged.anomalies.length} anomalies · {clientGroups.length} groups
          <ExportCsvButton
            filename="anomalies-grouped"
            headers={exportHeaders}
            rows={exportRows}
            testId="export-grouped-csv"
          />
        </span>
      </div>

      <RefetchingBlock isRefetching={flagged.isRefetching} className="space-y-3">
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
              {group.rows.slice(0, CLIENT_GROUP_ROW_PREVIEW).map((row) => {
                const detail = enrichmentFor(enrichment.byTimestamp, row.timestamp);
                const sensors = detail?.affectedSensors ?? [];
                return (
                  <AnomalyDetailRow
                    key={row.timestamp}
                    timestamp={row.timestamp}
                    score={row.score}
                    severity={row.severity}
                    sensors={sensors}
                    values={clientRowValues(row)}
                    reasoning={detail?.reasoning ?? null}
                    jumpChannels={sensors}
                  />
                );
              })}
            </ul>
          </details>
        ))}
      </RefetchingBlock>
    </div>
  );
}
