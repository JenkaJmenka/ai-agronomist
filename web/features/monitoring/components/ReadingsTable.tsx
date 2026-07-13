"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";

import { Chip } from "@/components/ui/Chip";
import { DataTable } from "@/components/ui/DataTable";
import type { SensorRow } from "@/features/monitoring/api/readings";
import { getSeverity } from "@/lib/severity";

interface ReadingsTableProps {
  readonly rows: SensorRow[];
  readonly channels: string[];
  /** Rows scoring at/above this are highlighted as anomalies. */
  readonly threshold: number;
}

function isAnomalousRow(row: SensorRow, threshold: number): boolean {
  return typeof row.anomaly_score === "number" && row.anomaly_score >= threshold;
}

/** Split a reading timestamp into a short localized date line and a seconds-free time line. */
function TimestampCell({ value }: { readonly value: unknown }) {
  const raw = String(value);
  const parts = raw.replace(" ", "T").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!parts) {
    return <span>{raw.replace("T", " ").slice(0, 19)}</span>;
  }
  const [, year, month, day, hour, minute] = parts;
  // Build from local parts so localization never shifts the displayed wall-clock time.
  const dt = new Date(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute));
  return (
    <div className="leading-tight">
      <div>
        {dt.toLocaleDateString(undefined, { day: "2-digit", month: "2-digit", year: "2-digit" })}
      </div>
      <div>{dt.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</div>
    </div>
  );
}

export function ReadingsTable({ rows, channels, threshold }: ReadingsTableProps) {
  const columns = useMemo<ColumnDef<SensorRow, unknown>[]>(() => {
    const base: ColumnDef<SensorRow, unknown>[] = [
      {
        header: "Timestamp",
        accessorKey: "timestamp",
        cell: (ctx) => <TimestampCell value={ctx.getValue()} />,
      },
      ...channels.map<ColumnDef<SensorRow, unknown>>((ch) => ({
        header: ch,
        accessorKey: ch,
        cell: (ctx) => {
          const v = ctx.getValue();
          return typeof v === "number" ? v.toFixed(2) : "—";
        },
      })),
      {
        header: "Score",
        accessorKey: "anomaly_score",
        cell: (ctx) => {
          const v = ctx.getValue();
          if (typeof v !== "number") return "—";
          const severity = getSeverity({ score: v, threshold });
          return <Chip severity={severity}>{v.toFixed(3)}</Chip>;
        },
      },
    ];
    return base;
  }, [channels, threshold]);

  // Newest first; DataTable virtualizes, so the full window renders uncapped.
  const recentRows = useMemo(() => [...rows].reverse(), [rows]);

  return (
    <DataTable
      columns={columns}
      rows={recentRows}
      scrollX
      rowClassName={(row) => (isAnomalousRow(row, threshold) ? "bg-sev-critical/10" : undefined)}
    />
  );
}
