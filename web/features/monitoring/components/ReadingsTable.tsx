"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo } from "react";

import { Chip } from "@/components/ui/Chip";
import { DataTable } from "@/components/ui/DataTable";
import { ExportCsvButton } from "@/components/ui/ExportCsvButton";
import type { SensorRow } from "@/features/monitoring/api/readings";
import type { Setpoints } from "@/features/monitoring/hooks/useSetpoints";
import { getChannelStatus } from "@/features/monitoring/lib/channelMeta";
import { getSeverity } from "@/lib/severity";

interface ReadingsTableProps {
  readonly rows: SensorRow[];
  readonly channels: string[];
  /** Rows scoring at/above this are treated as anomalous. */
  readonly threshold: number;
  /** Effective setpoints — used to decide which channel cells to highlight. */
  readonly setpoints: Setpoints;
}

function isAnomalousRow(row: SensorRow, threshold: number): boolean {
  return typeof row.anomaly_score === "number" && row.anomaly_score >= threshold;
}

/** Channel cell is anomalous when the row is flagged and the value is off-setpoint. */
function isAnomalousChannel(
  row: SensorRow,
  channel: string,
  threshold: number,
  setpoints: Setpoints,
): boolean {
  if (!isAnomalousRow(row, threshold)) return false;
  const value = row[channel];
  if (typeof value !== "number") return false;
  return getChannelStatus(channel, value, setpoints) !== "ok";
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

const ANOMALY_CELL = "bg-sev-critical/15";

export function ReadingsTable({ rows, channels, threshold, setpoints }: ReadingsTableProps) {
  const columns = useMemo<ColumnDef<SensorRow, unknown>[]>(() => {
    const base: ColumnDef<SensorRow, unknown>[] = [
      {
        header: "Timestamp",
        accessorKey: "timestamp",
        cell: (ctx) => <TimestampCell value={ctx.getValue()} />,
      },
      ...channels.map<ColumnDef<SensorRow, unknown>>((ch) => ({
        id: ch,
        header: ch,
        accessorKey: ch,
        cell: (ctx) => {
          const v = ctx.getValue();
          return typeof v === "number" ? v.toFixed(2) : "—";
        },
      })),
      {
        id: "anomaly_score",
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

  // Newest first; DataTable caps body height (scrollY) and virtualizes large windows.
  const recentRows = useMemo(() => [...rows].reverse(), [rows]);

  const exportRows = useMemo(() => {
    return recentRows.map((row) => [
      row.timestamp,
      ...channels.map((ch) => {
        const v = row[ch];
        return typeof v === "number" ? v : "";
      }),
      typeof row.anomaly_score === "number" ? row.anomaly_score : "",
    ]);
  }, [recentRows, channels]);

  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <ExportCsvButton
          filename="sensor-readings"
          headers={["timestamp", ...channels, "anomaly_score"]}
          rows={exportRows}
          testId="export-readings-csv"
        />
      </div>
      <DataTable
        columns={columns}
        rows={recentRows}
        scrollX
        scrollY
        cellClassName={(row, columnId) => {
          if (columnId === "anomaly_score") {
            return isAnomalousRow(row, threshold) ? ANOMALY_CELL : undefined;
          }
          if (
            channels.includes(columnId) &&
            isAnomalousChannel(row, columnId, threshold, setpoints)
          ) {
            return ANOMALY_CELL;
          }
          return undefined;
        }}
      />
    </div>
  );
}
