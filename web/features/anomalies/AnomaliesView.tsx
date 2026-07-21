"use client";

import { Suspense, useMemo, useState } from "react";

import { Card } from "@/components/ui/Card";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { SERVER_GROUPING_THRESHOLD } from "@/features/anomalies/api/flaggedWindow";
import { AnomalyTable } from "@/features/anomalies/components/AnomalyTable";
import { GroupedAnomalies } from "@/features/anomalies/components/GroupedAnomalies";
import { ScoreTimeline } from "@/features/anomalies/components/ScoreTimeline";
import { ThresholdControl } from "@/features/anomalies/components/ThresholdControl";
import { useAnomalies } from "@/features/anomalies/hooks/useAnomalies";
import { useEnrichedAnomalies } from "@/features/anomalies/hooks/useEnrichedAnomalies";
import { discoverChannels } from "@/features/monitoring/api/readings";
import { useSetpoints } from "@/features/monitoring/hooks/useSetpoints";
import { SharedViewControls } from "@/features/shell/SharedViewControls";
import { useDateRange } from "@/lib/useDateRange";

type ViewMode = "graphical" | "table";

function AnomaliesBody() {
  const {
    rows,
    anomalies,
    threshold,
    setThreshold,
    isLoading,
    isRefetching,
    isError,
    error,
    windowLabel,
    downsampled,
    originalCount,
  } = useAnomalies();
  const [view, setView] = useState<ViewMode>("graphical");
  const [, setRange] = useDateRange();
  const { setpoints, updateSetpoint, resetToDefaults } = useSetpoints();
  const channels = useMemo(() => discoverChannels(rows), [rows]);

  // Envelope bounds drive the G4/G3 queries — rows are timestamp-ordered.
  const windowStart = useMemo(() => rows[0]?.timestamp ?? null, [rows]);
  const windowEnd = useMemo(() => rows.at(-1)?.timestamp ?? null, [rows]);
  // Fetch up to the API max so "All anomalies" can list every flagged row
  // (Top anomalies still slices to 50 client-side). Chart rows alone undercount.
  const enrichment = useEnrichedAnomalies({
    windowStart,
    windowEnd,
    threshold,
    enabled: rows.length > 0,
    limit: SERVER_GROUPING_THRESHOLD,
  });
  const anomalyTotal = enrichment.anomalyCount ?? anomalies.length;
  const tableAnomalies = enrichment.topRows ?? anomalies;

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
        <SharedViewControls
          channels={channels}
          setpoints={setpoints}
          onUpdateSetpoint={updateSetpoint}
          onResetSetpoints={resetToDefaults}
        />
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
            <span className="font-semibold">{anomalyTotal}</span>
            <span className="text-text-muted"> anomalies ≥ {threshold.toFixed(3)} in </span>
            <span className="font-semibold">{originalCount.toLocaleString()}</span>
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
        <RefetchingBlock isRefetching={isRefetching} className="space-y-6">
          <Card title="Anomaly score timeline">
            <ScoreTimeline
              rows={rows}
              threshold={threshold}
              // Drag-selecting a span narrows the URL window, so the anomaly
              // tables below filter to the same range — and the Period control.
              onRangeSelect={(from, to) => void setRange({ from, to })}
            />
          </Card>
          <Card title="Top anomalies">
            <AnomalyTable
              mode="top"
              anomalies={tableAnomalies}
              enrichment={enrichment.byTimestamp}
              isEnrichmentLoading={enrichment.isLoading}
            />
          </Card>
          <Card
            title={
              anomalyTotal > 0
                ? `All anomalies (${anomalyTotal.toLocaleString()})`
                : "All anomalies"
            }
          >
            <AnomalyTable
              mode="all"
              anomalies={tableAnomalies}
              enrichment={enrichment.byTimestamp}
              isEnrichmentLoading={enrichment.isLoading}
            />
          </Card>
        </RefetchingBlock>
      ) : (
        <RefetchingBlock isRefetching={isRefetching}>
          <Card title="Detected anomalies (grouped)">
            <GroupedAnomalies
              anomalyCount={anomalyTotal}
              windowStart={windowStart}
              windowEnd={windowEnd}
              threshold={threshold}
            />
          </Card>
        </RefetchingBlock>
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
