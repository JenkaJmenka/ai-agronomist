"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";

import { Chip } from "@/components/ui/Chip";
import { DataTable } from "@/components/ui/DataTable";
import { ExportCsvButton } from "@/components/ui/ExportCsvButton";
import { Skeleton } from "@/components/ui/Skeleton";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import type { AnomalyEnrichment } from "@/features/anomalies/hooks/useEnrichedAnomalies";
import { FAULT_LABELS } from "@/lib/severity";
import {
  type SelectionWindowHandler,
  useTimestampRowSelection,
} from "@/lib/useTimestampRowSelection";

/** Cap for the "Top anomalies" table (highest scores). */
export const TOP_ANOMALIES_CAP = 50;

const SEVERITY_LABEL = {
  normal: "Normal",
  warning: "Warning",
  lstm: "LSTM / drift",
  critical: "Critical",
} as const;

type AnomalyTableMode = "top" | "all";

interface AnomalyTableProps {
  readonly anomalies: readonly AnomalyRow[];
  /** Enrichment keyed by row timestamp; absent while loading/unavailable. */
  readonly enrichment?: ReadonlyMap<string, AnomalyEnrichment>;
  /** Fault-type cells show skeletons instead of "—" while enrichment loads. */
  readonly isEnrichmentLoading?: boolean;
  /**
   * `top` — highest-scoring rows (capped at {@link TOP_ANOMALIES_CAP}).
   * `all` — every passed row, newest-first (paginated by DataTable).
   */
  readonly mode?: AnomalyTableMode;
  /**
   * Selecting rows updates the shared URL window so the score timeline
   * zooms to the same span (click / ⌘·Ctrl-click / Shift-click).
   */
  readonly onWindowSelect?: SelectionWindowHandler;
}

function faultLabel(faultType: string | null | undefined): string | null {
  if (!faultType) return null;
  return FAULT_LABELS[faultType] ?? faultType.replaceAll("_", " ");
}

function byTimestampDesc(a: AnomalyRow, b: AnomalyRow): number {
  return a.timestamp < b.timestamp ? 1 : a.timestamp > b.timestamp ? -1 : 0;
}

/** Anomaly rows table — top-by-score or full list, both newest-first on screen. */
export function AnomalyTable({
  anomalies,
  enrichment,
  isEnrichmentLoading = false,
  mode = "top",
  onWindowSelect,
}: AnomalyTableProps) {
  const { isSelected, handleRowClick } = useTimestampRowSelection(onWindowSelect);

  const columns = useMemo<ColumnDef<AnomalyRow, unknown>[]>(
    () => [
      {
        header: "Timestamp",
        accessorKey: "timestamp",
        cell: (ctx) => (
          <span className="tabular-nums">
            {String(ctx.getValue()).replace("T", " ").slice(0, 19)}
          </span>
        ),
      },
      {
        header: "Score",
        accessorKey: "score",
        cell: (ctx) => (
          <span className="tabular-nums">{(ctx.getValue() as number).toFixed(4)}</span>
        ),
      },
      {
        header: "Severity",
        accessorKey: "severity",
        cell: (ctx) => {
          const severity = ctx.getValue() as AnomalyRow["severity"];
          return <Chip severity={severity}>{SEVERITY_LABEL[severity]}</Chip>;
        },
      },
      {
        header: "Fault type",
        id: "fault_type",
        cell: (ctx) => {
          const detail = enrichment?.get(ctx.row.original.timestamp);
          const label = faultLabel(detail?.faultType);
          if (!label) {
            if (isEnrichmentLoading) return <Skeleton className="h-4 w-24" />;
            return <span className="text-text-muted">—</span>;
          }
          return (
            <span title={detail?.rootCause ?? undefined} data-testid="fault-type">
              {label}
            </span>
          );
        },
      },
      {
        header: "Why",
        id: "reasoning",
        cell: (ctx) => {
          const detail = enrichment?.get(ctx.row.original.timestamp);
          const reasoning = detail?.reasoning;
          if (!reasoning) {
            if (isEnrichmentLoading) return <Skeleton className="h-4 w-32" />;
            return <span className="text-text-muted">—</span>;
          }
          return (
            <span
              className="block max-w-xs whitespace-pre-line break-words text-text-muted"
              title={reasoning}
              data-testid="reasoning"
            >
              {reasoning}
            </span>
          );
        },
      },
      {
        header: "Actions",
        id: "actions",
        cell: (ctx) => {
          const detail = enrichment?.get(ctx.row.original.timestamp);
          return (
            <MonitoringJumpLink
              timestamp={ctx.row.original.timestamp}
              channels={detail?.affectedSensors ?? null}
            />
          );
        },
      },
    ],
    [enrichment, isEnrichmentLoading],
  );

  const displayRows = useMemo(() => {
    if (mode === "all") {
      return [...anomalies].sort(byTimestampDesc);
    }
    // Top: keep the highest-scoring rows, then show them newest → oldest.
    return [...anomalies]
      .sort((a, b) => b.score - a.score)
      .slice(0, TOP_ANOMALIES_CAP)
      .sort(byTimestampDesc);
  }, [anomalies, mode]);

  const exportRows = useMemo(() => {
    return displayRows.map((row) => {
      const detail = enrichment?.get(row.timestamp);
      return [
        row.timestamp,
        row.score,
        SEVERITY_LABEL[row.severity],
        faultLabel(detail?.faultType) ?? "",
        detail?.reasoning ?? "",
        (detail?.affectedSensors ?? []).join(", "),
      ];
    });
  }, [displayRows, enrichment]);

  const exportFilename = mode === "all" ? "all-anomalies" : "top-anomalies";
  const exportTestId = mode === "all" ? "export-all-anomalies-csv" : "export-anomalies-csv";

  return (
    <div
      className="space-y-2"
      data-testid={mode === "all" ? "all-anomalies-table" : "top-anomalies-table"}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        {onWindowSelect ? (
          <p className="text-xs text-text-muted">
            Click a row to zoom the chart · ⌘/Ctrl-click adds · Shift-click selects a range
          </p>
        ) : (
          <span />
        )}
        <ExportCsvButton
          filename={exportFilename}
          headers={["timestamp", "score", "severity", "fault_type", "reasoning", "sensors"]}
          rows={exportRows}
          testId={exportTestId}
        />
      </div>
      <DataTable
        columns={columns}
        rows={displayRows}
        alignTop
        emptyMessage="No anomalies above the threshold in this window. Widen the range or lower the threshold."
        isRowSelected={onWindowSelect ? isSelected : undefined}
        onRowClick={
          onWindowSelect
            ? (row, event) => {
                handleRowClick(row, event, displayRows);
              }
            : undefined
        }
      />
    </div>
  );
}
