"use client";

import { type ReactNode, useEffect, useMemo, useState } from "react";

import { Chip } from "@/components/ui/Chip";
import { ExportCsvButton } from "@/components/ui/ExportCsvButton";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { paginateItems, TABLE_PAGE_SIZE, TablePagination } from "@/components/ui/TablePagination";
import type {
  AnomalyGroup,
  AnomalyGroupSample,
  ServerGroupBy,
} from "@/features/anomalies/api/enriched";
import { SERVER_GROUPING_THRESHOLD } from "@/features/anomalies/api/flaggedWindow";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import { useAnomalyCount } from "@/features/anomalies/hooks/useAnomalyCount";
import { useAnomalyGroups } from "@/features/anomalies/hooks/useAnomalyGroups";
import {
  type AnomalyEnrichment,
  useEnrichedAnomalies,
} from "@/features/anomalies/hooks/useEnrichedAnomalies";
import { useFlaggedWindow } from "@/features/anomalies/hooks/useFlaggedWindow";
import { fetchErrorHint } from "@/features/anomalies/lib/classifyFetchError";
import {
  formatSensorList,
  formatSensorValues,
} from "@/features/anomalies/lib/formatAnomalyDetails";
import { resolveGroupingMode } from "@/features/anomalies/lib/groupingDecision";
import { FAULT_LABELS } from "@/lib/severity";

const SERVER_GROUP_LABELS: Readonly<Record<ServerGroupBy, string>> = {
  date: "Date",
  type: "Fault type",
  sensor: "Sensor",
};

type ClientGroupBy = "date" | "severity";

interface GroupedAnomaliesProps {
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
  const groups = [...buckets.entries()].map(([key, rows]) => ({
    key,
    // Newest first within each group.
    rows: [...rows].sort((a, b) =>
      a.timestamp < b.timestamp ? 1 : a.timestamp > b.timestamp ? -1 : 0,
    ),
    peak: Math.max(...rows.map((r) => r.score)),
  }));
  if (groupBy === "date") {
    // Date groups newest calendar day first.
    return groups.sort((a, b) => (a.key < b.key ? 1 : a.key > b.key ? -1 : 0));
  }
  return groups.sort((a, b) => b.peak - a.peak);
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
  /**
   * When true, enrichment data (sensors, reasoning) is still being fetched.
   * Renders a loading skeleton instead of an empty/dash placeholder so an
   * in-flight 43 s request can never be mistaken for missing data.
   */
  readonly isEnrichmentLoading?: boolean;
}) {
  const sensorLabel = formatSensorList(props.sensors);
  const valueLabel = formatSensorValues(props.values, props.sensors);
  const showSensorSkeleton = (props.isEnrichmentLoading ?? false) && props.sensors.length === 0;
  const showReasoningSkeleton = (props.isEnrichmentLoading ?? false) && props.reasoning === null;

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
        <span data-testid="grouped-anomaly-sensors">
          {showSensorSkeleton ? <Skeleton className="h-3.5 w-32" /> : sensorLabel}
        </span>
        <span className="font-medium text-text">Values</span>
        <span className="tabular-nums" data-testid="grouped-anomaly-values">
          {valueLabel || "—"}
        </span>
        <span className="font-medium text-text">Reasoning</span>
        <span data-testid="grouped-anomaly-reasoning">
          {showReasoningSkeleton ? (
            <Skeleton className="h-3.5 w-48" />
          ) : (
            props.reasoning?.trim() || "—"
          )}
        </span>
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

/** Paginate nested sample/row lists inside a single group. */
function PaginatedList<T>({
  items,
  resetKey,
  renderItem,
}: {
  readonly items: readonly T[];
  readonly resetKey: string;
  readonly renderItem: (item: T) => ReactNode;
}) {
  const [pageIndex, setPageIndex] = useState(0);
  useEffect(() => {
    setPageIndex(0);
  }, [resetKey, items.length]);

  const {
    pageItems,
    pageIndex: safePage,
    pageCount,
  } = paginateItems(items, pageIndex, TABLE_PAGE_SIZE);

  return (
    <div className="space-y-2">
      <ul className="divide-y divide-border border-t border-border">{pageItems.map(renderItem)}</ul>
      <div className="px-4 pb-3">
        <TablePagination
          pageIndex={safePage}
          pageCount={pageCount}
          totalRows={items.length}
          onPageChange={setPageIndex}
          label="records"
        />
      </div>
    </div>
  );
}

function ServerGroupedList(props: {
  readonly groups: readonly AnomalyGroup[];
  readonly groupsPageIndex: number;
  readonly onGroupsPageChange: (pageIndex: number) => void;
  readonly serverGroupBy: ServerGroupBy;
  readonly isRefetching: boolean;
}) {
  const {
    pageItems: pageGroups,
    pageIndex: safeGroupsPage,
    pageCount: groupsPageCount,
  } = paginateItems(props.groups, props.groupsPageIndex, TABLE_PAGE_SIZE);

  return (
    <RefetchingBlock isRefetching={props.isRefetching} className="space-y-3">
      {pageGroups.map((group) => {
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
              {props.serverGroupBy === "type" ? (FAULT_LABELS[group.key] ?? group.key) : group.key}{" "}
              · {group.count.toLocaleString()} record{group.count === 1 ? "" : "s"} · peak{" "}
              <span className="tabular-nums">{group.top_score.toFixed(3)}</span>
            </summary>
            <PaginatedList
              items={samples}
              resetKey={`${group.key}:${samples.length}`}
              renderItem={(sample) => (
                <AnomalyDetailRow
                  key={sample.timestamp}
                  timestamp={sample.timestamp}
                  score={sample.anomaly_score ?? null}
                  sensors={
                    props.serverGroupBy === "sensor" && sample.affected_sensors.length === 0
                      ? [group.key]
                      : sample.affected_sensors
                  }
                  values={sample.values}
                  reasoning={sample.reasoning ?? null}
                  jumpChannels={
                    props.serverGroupBy === "sensor" ? [group.key] : sample.affected_sensors
                  }
                />
              )}
            />
          </details>
        );
      })}
      <TablePagination
        pageIndex={safeGroupsPage}
        pageCount={groupsPageCount}
        totalRows={props.groups.length}
        onPageChange={props.onGroupsPageChange}
        label="groups"
      />
    </RefetchingBlock>
  );
}

/**
 * Grouped table view (plan §6.2 AnomalyGroupedTable). Under
 * SERVER_GROUPING_THRESHOLD flagged rows grouping is client compute over the
 * exact G2 flagged rows; above it the G3 endpoint aggregates server-side.
 *
 * The routing decision is derived from the cheap count fetch
 * (/readings/anomalies-in-window?max_points=1), NOT from the slow G4 enriched
 * endpoint. This breaks the circular dependency that previously caused the
 * client path to activate prematurely (enrichment count null → fallback to
 * downsampled 35 → 35 < 500 → client path → enrichment misses → blank cells).
 */
export function GroupedAnomalies({ windowStart, windowEnd, threshold }: GroupedAnomaliesProps) {
  const [clientGroupBy, setClientGroupBy] = useState<ClientGroupBy>("date");
  const [serverGroupBy, setServerGroupBy] = useState<ServerGroupBy>("date");

  // Always-enabled cheap count fetch — breaks the circularity.
  const countState = useAnomalyCount({
    windowStart,
    windowEnd,
    threshold,
    enabled: windowStart !== null && windowEnd !== null,
  });
  const mode = resolveGroupingMode(countState.count);
  const isServer = mode === "server";

  const serverState = useAnomalyGroups({
    windowStart,
    windowEnd,
    threshold,
    groupBy: serverGroupBy,
    enabled: isServer,
  });
  // useFlaggedWindow is now gated on mode === "client" (not !useServer).
  // Previously it was enabled: !useServer, but useServer itself depended on
  // anomalyCount which came from the slow enriched endpoint — circular.
  const flagged = useFlaggedWindow({
    windowStart,
    windowEnd,
    threshold,
    enabled: mode === "client",
  });
  // Enrichment for client groups — same window, up to the client-side cap.
  const enrichment = useEnrichedAnomalies({
    windowStart,
    windowEnd,
    threshold,
    enabled: mode === "client" && flagged.anomalies.length > 0,
    limit: SERVER_GROUPING_THRESHOLD,
  });
  const clientGroups = useMemo(
    () => (isServer ? [] : groupClientSide(flagged.anomalies, clientGroupBy)),
    [flagged.anomalies, clientGroupBy, isServer],
  );

  const [groupsPageIndex, setGroupsPageIndex] = useState(0);
  useEffect(() => {
    setGroupsPageIndex(0);
  }, [clientGroupBy, serverGroupBy, isServer, countState.count, flagged.anomalies.length]);

  const exportRows = useMemo(() => {
    if (isServer) {
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
  }, [isServer, serverState.groups, clientGroups, enrichment.byTimestamp]);

  // Show a loading skeleton while the cheap count fetch is in flight so we
  // never render either grouping path before the routing decision is valid.
  if (countState.isLoading) {
    return <Skeleton className="h-40 w-full" />;
  }

  if (countState.isError || countState.count === null) {
    const hint = fetchErrorHint(countState.error);
    return (
      <p role="alert" className="py-4 text-center text-sm text-sev-critical">
        Could not determine anomaly count ({hint}).
      </p>
    );
  }

  // countState.count is now a number; TypeScript narrowed via the null guard above.
  const exactCount = countState.count;

  if (mode === "client" && flagged.isLoading) {
    return <Skeleton className="h-40 w-full" />;
  }

  if (mode === "client" && flagged.isError) {
    const hint = fetchErrorHint(flagged.error);
    return (
      <p role="alert" className="py-4 text-center text-sm text-sev-critical">
        Could not load the window&apos;s anomalies ({hint}).
      </p>
    );
  }

  if (mode === "client" && flagged.anomalies.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-text-muted">
        No anomalies to group in this window.
      </p>
    );
  }

  const exportHeaders = ["group", "timestamp", "score", "sensors", "values", "reasoning"] as const;

  if (isServer) {
    return (
      <div className="space-y-3" data-testid="server-grouped">
        <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="group-by">
          <span className="text-text-muted">Group by</span>
          {(Object.keys(SERVER_GROUP_LABELS) as ServerGroupBy[]).map((m) => (
            <GroupModeButton
              key={m}
              label={SERVER_GROUP_LABELS[m]}
              isActive={serverGroupBy === m}
              onClick={() => setServerGroupBy(m)}
            />
          ))}
          <span className="ml-auto flex items-center gap-2 text-xs text-text-muted tabular-nums">
            {exactCount.toLocaleString()} anomalies · grouped server-side
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
            Could not load server groups ({fetchErrorHint(serverState.error)}).
          </p>
        ) : (
          <ServerGroupedList
            groups={serverState.groups}
            groupsPageIndex={groupsPageIndex}
            onGroupsPageChange={setGroupsPageIndex}
            serverGroupBy={serverGroupBy}
            isRefetching={serverState.isRefetching}
          />
        )}
      </div>
    );
  }

  const {
    pageItems: pageClientGroups,
    pageIndex: safeClientGroupsPage,
    pageCount: clientGroupsPageCount,
  } = paginateItems(clientGroups, groupsPageIndex, TABLE_PAGE_SIZE);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="group-by">
        <span className="text-text-muted">Group by</span>
        {(["date", "severity"] as const).map((m) => (
          <GroupModeButton
            key={m}
            label={m === "date" ? "Date" : "Severity"}
            isActive={clientGroupBy === m}
            onClick={() => setClientGroupBy(m)}
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
        {pageClientGroups.map((group) => (
          <details
            key={group.key}
            className="rounded-[var(--radius-card)] border border-border bg-bg-elevated"
            open={clientGroups.length <= 3}
          >
            <summary className="cursor-pointer px-4 py-2.5 text-sm font-medium">
              {group.key} · {group.rows.length} record{group.rows.length === 1 ? "" : "s"} · peak{" "}
              <span className="tabular-nums">{group.peak.toFixed(3)}</span>
            </summary>
            <PaginatedList
              items={group.rows}
              resetKey={`${group.key}:${group.rows.length}`}
              renderItem={(row) => {
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
                    isEnrichmentLoading={enrichment.isLoading}
                  />
                );
              }}
            />
          </details>
        ))}
        <TablePagination
          pageIndex={safeClientGroupsPage}
          pageCount={clientGroupsPageCount}
          totalRows={clientGroups.length}
          onPageChange={setGroupsPageIndex}
          label="groups"
        />
      </RefetchingBlock>
    </div>
  );
}
