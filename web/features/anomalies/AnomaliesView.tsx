"use client";

import { Suspense, useMemo, useState } from "react";

import { Card } from "@/components/ui/Card";
import { DateRangeControls } from "@/components/ui/DateRangeControls";
import { Skeleton } from "@/components/ui/Skeleton";
import { AnomalyTable } from "@/features/anomalies/components/AnomalyTable";
import { GroupedAnomalies } from "@/features/anomalies/components/GroupedAnomalies";
import { ScoreTimeline } from "@/features/anomalies/components/ScoreTimeline";
import { ThresholdControl } from "@/features/anomalies/components/ThresholdControl";
import { useAnomalies } from "@/features/anomalies/hooks/useAnomalies";
import { useEnrichedAnomalies } from "@/features/anomalies/hooks/useEnrichedAnomalies";

type ViewMode = "graphical" | "table";

function AnomaliesBody() {
  const {
    rows,
    anomalies,
    threshold,
    setThreshold,
    isLoading,
    isError,
    error,
    windowLabel,
    downsampled,
  } = useAnomalies();
  const [view, setView] = useState<ViewMode>("graphical");

  // Envelope bounds drive the G4/G3 queries — rows are timestamp-ordered.
  const windowStart = useMemo(() => rows[0]?.timestamp ?? null, [rows]);
  const windowEnd = useMemo(() => rows.at(-1)?.timestamp ?? null, [rows]);
  const enrichment = useEnrichedAnomalies({
    windowStart,
    windowEnd,
    threshold,
    enabled: anomalies.length > 0,
  });

  if (isError) {
    throw error ?? new Error("Failed to load anomalies");
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Anomaly Detection</h1>
          <p className="text-sm text-text-muted tabular-nums">
            {windowLabel}
            {downsampled ? " · downsampled (anomalies preserved)" : ""}
          </p>
        </div>
        <DateRangeControls />
      </div>

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <ThresholdControl threshold={threshold} onChange={setThreshold} />
          <div className="flex gap-1" role="tablist" aria-label="View">
            {(["graphical", "table"] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                role="tab"
                aria-selected={view === mode}
                onClick={() => setView(mode)}
                className={`rounded-[var(--radius-control)] border px-3 py-1.5 text-sm ${
                  view === mode
                    ? "border-accent font-medium text-accent"
                    : "border-border text-text-muted hover:text-text"
                }`}
              >
                {mode === "graphical" ? "Graphical view" : "Table view"}
              </button>
            ))}
          </div>
          <p className="text-sm tabular-nums">
            <span className="font-semibold">{anomalies.length}</span>
            <span className="text-text-muted"> anomalies ≥ {threshold.toFixed(3)} in </span>
            <span className="font-semibold">{rows.length.toLocaleString()}</span>
            <span className="text-text-muted"> points</span>
          </p>
        </div>
      </Card>

      {isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-48 w-full" />
        </div>
      ) : rows.length === 0 ? (
        <Card>
          <p className="py-8 text-center text-sm text-text-muted">
            No sensor readings in the selected window. Widen the range or pick another
            tenant/greenhouse.
          </p>
        </Card>
      ) : view === "graphical" ? (
        <>
          <Card title="Anomaly score timeline">
            <ScoreTimeline rows={rows} threshold={threshold} />
          </Card>
          <Card title="Top anomalies">
            <AnomalyTable anomalies={anomalies} enrichment={enrichment.byTimestamp} />
          </Card>
        </>
      ) : (
        <Card title="Detected anomalies (grouped)">
          <GroupedAnomalies
            anomalies={anomalies}
            windowStart={windowStart}
            windowEnd={windowEnd}
            threshold={threshold}
          />
        </Card>
      )}
    </div>
  );
}

export function AnomaliesView() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-64 w-full" />
        </div>
      }
    >
      <AnomaliesBody />
    </Suspense>
  );
}
