"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";

import { Chip } from "@/components/ui/Chip";
import { DataTable } from "@/components/ui/DataTable";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";

const TOP_ANOMALIES_CAP = 50;

const SEVERITY_LABEL = {
  normal: "Normal",
  warning: "Warning",
  lstm: "LSTM / drift",
  critical: "Critical",
} as const;

interface AnomalyTableProps {
  readonly anomalies: AnomalyRow[];
}

/** Top anomalies, most severe first, with the Monitoring jump per row. */
export function AnomalyTable({ anomalies }: AnomalyTableProps) {
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
        header: "Actions",
        id: "actions",
        cell: (ctx) => <MonitoringJumpLink timestamp={ctx.row.original.timestamp} />,
      },
    ],
    [],
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
