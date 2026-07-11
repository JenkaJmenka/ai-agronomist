"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";

import { Chip } from "@/components/ui/Chip";
import { DataTable } from "@/components/ui/DataTable";
import type { SensorRow } from "@/features/monitoring/api/readings";
import { getSeverity } from "@/lib/severity";

const TABLE_ROW_CAP = 200;
const SCORE_THRESHOLD_FALLBACK = 0.15;

interface ReadingsTableProps {
  readonly rows: SensorRow[];
  readonly channels: string[];
}

export function ReadingsTable({ rows, channels }: ReadingsTableProps) {
  const columns = useMemo<ColumnDef<SensorRow, unknown>[]>(() => {
    const base: ColumnDef<SensorRow, unknown>[] = [
      {
        header: "Timestamp",
        accessorKey: "timestamp",
        cell: (ctx) => String(ctx.getValue()).replace("T", " ").slice(0, 19),
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
          const severity = getSeverity({ score: v, threshold: SCORE_THRESHOLD_FALLBACK });
          return <Chip severity={severity}>{v.toFixed(3)}</Chip>;
        },
      },
    ];
    return base;
  }, [channels]);

  // Newest first; capped until virtualization (P1 follow-up) lands.
  const recentRows = useMemo(() => rows.slice(-TABLE_ROW_CAP).reverse(), [rows]);

  return <DataTable columns={columns} rows={recentRows} />;
}
