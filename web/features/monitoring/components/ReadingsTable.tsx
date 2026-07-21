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
  /** Effective setpoints — fallback when enrichment has no affected sensors. */
  readonly setpoints: Setpoints;
  /**
   * Detector-attributed sensors keyed by reading timestamp.
   * When present, only these channel cells highlight on a flagged row.
   */
  readonly affectedByTimestamp?: ReadonlyMap<string, readonly string[]>;
}

export function isAnomalousRow(row: SensorRow, threshold: number): boolean {
  return typeof row.anomaly_score === "number" && row.anomaly_score >= threshold;
}

/** Map detector sensor names onto columns currently shown in the table. */
export function resolveAffectedChannels(
  affected: readonly string[],
  channels: readonly string[],
): string[] {
  if (affected.length === 0 || channels.length === 0) return [];
  const channelSet = new Set(channels);
  const out: string[] = [];
  for (const sensor of affected) {
    if (channelSet.has(sensor)) {
      if (!out.includes(sensor)) out.push(sensor);
      continue;
    }
    const hit = channels.find(
      (ch) =>
        ch.toLowerCase() === sensor.toLowerCase() ||
        ch.toLowerCase().includes(sensor.toLowerCase()) ||
        sensor.toLowerCase().includes(ch.toLowerCase()),
    );
    if (hit && !out.includes(hit)) out.push(hit);
  }
  return out;
}

/**
 * Channels to highlight on a flagged row — never the whole row.
 *
 * 1. Detector `affected_sensors` / live `affected_channels` (when known)
 * 2. Else off-setpoint channels only
 * 3. Else none (score cell still highlights)
 */
export function anomalousChannelsForRow(
  row: SensorRow,
  channels: readonly string[],
  threshold: number,
  setpoints: Setpoints,
  affectedSensors?: readonly string[] | null,
): readonly string[] {
  if (!isAnomalousRow(row, threshold)) return [];

  if (affectedSensors && affectedSensors.length > 0) {
    return resolveAffectedChannels(affectedSensors, channels).filter(
      (ch) => typeof row[ch] === "number",
    );
  }

  return channels.filter((ch) => {
    const value = row[ch];
    return typeof value === "number" && getChannelStatus(ch, value, setpoints) !== "ok";
  });
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

const ANOMALY_CELL = "bg-sev-critical/20";

/** Normalize ISO timestamps so enrichment / live / reading keys align. */
function timestampKey(ts: string): string {
  const ms = Date.parse(ts);
  return Number.isFinite(ms) ? new Date(ms).toISOString() : ts;
}

export function ReadingsTable({
  rows,
  channels,
  threshold,
  setpoints,
  affectedByTimestamp,
}: ReadingsTableProps) {
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

  // Explicit newest-first by timestamp (don't assume API/chart row order).
  const recentRows = useMemo(
    () =>
      [...rows].sort((a, b) =>
        a.timestamp < b.timestamp ? 1 : a.timestamp > b.timestamp ? -1 : 0,
      ),
    [rows],
  );

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

  const highlightByTs = useMemo(() => {
    const map = new Map<string, ReadonlySet<string>>();
    for (const row of recentRows) {
      const key = timestampKey(row.timestamp);
      const affected =
        affectedByTimestamp?.get(row.timestamp) ??
        affectedByTimestamp?.get(key) ??
        null;
      map.set(
        row.timestamp,
        new Set(anomalousChannelsForRow(row, channels, threshold, setpoints, affected)),
      );
    }
    return map;
  }, [recentRows, channels, threshold, setpoints, affectedByTimestamp]);

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
          if (highlightByTs.get(row.timestamp)?.has(columnId)) {
            return ANOMALY_CELL;
          }
          return undefined;
        }}
      />
    </div>
  );
}
