"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";

import { Chip } from "@/components/ui/Chip";
import { DataTable } from "@/components/ui/DataTable";
import { Skeleton } from "@/components/ui/Skeleton";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import type { AnomalyEnrichment } from "@/features/anomalies/hooks/useEnrichedAnomalies";
import { FAULT_LABELS } from "@/lib/severity";

const TOP_ANOMALIES_CAP = 50;

const SEVERITY_LABEL = {
  normal: "Normal",
  warning: "Warning",
  lstm: "LSTM / drift",
  critical: "Critical",
} as const;

interface AnomalyTableProps {
  readonly anomalies: readonly AnomalyRow[];
  /** G4 enrichment keyed by row timestamp; absent while loading/unavailable. */
  readonly enrichment?: ReadonlyMap<string, AnomalyEnrichment>;
  /** Fault-type cells show skeletons instead of "—" while enrichment loads. */
  readonly isEnrichmentLoading?: boolean;
}

function faultLabel(faultType: string | null | undefined): string | null {
  if (!faultType) return null;
  return FAULT_LABELS[faultType] ?? faultType.replaceAll("_", " ");
}

/** Top anomalies, most severe first, with fault type + Monitoring jump. */
export function AnomalyTable({
  anomalies,
  enrichment,
  isEnrichmentLoading = false,
}: AnomalyTableProps) {
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

  const top = useMemo(() => anomalies.slice(0, TOP_ANOMALIES_CAP), [anomalies]);

  return (
    <DataTable
      columns={columns}
      rows={top}
      emptyMessage="No anomalies above the threshold in this window. Widen the range or lower the threshold."
    />
  );
}
