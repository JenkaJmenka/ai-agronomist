"use client";

import { Suspense, useMemo, useState } from "react";

import { Card } from "@/components/ui/Card";
import { DateRangeControls } from "@/components/ui/DateRangeControls";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { AnomalyTable } from "@/features/anomalies/components/AnomalyTable";
import { GroupedAnomalies } from "@/features/anomalies/components/GroupedAnomalies";
import { ScoreTimeline } from "@/features/anomalies/components/ScoreTimeline";
import { ThresholdControl } from "@/features/anomalies/components/ThresholdControl";
import { useAnomalies } from "@/features/anomalies/hooks/useAnomalies";
import { useEnrichedAnomalies } from "@/features/anomalies/hooks/useEnrichedAnomalies";
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

  // Envelope bounds drive the G4/G3 queries — rows are timestamp-ordered.
  const windowStart = useMemo(() => rows[0]?.timestamp ?? null, [rows]);
  const windowEnd = useMemo(() => rows.at(-1)?.timestamp ?? null, [rows]);
  // Enabled whenever the window has rows (not just flagged ones): the server
  // count is the total's source of truth, since client rows are downsampled
  // and switching ranges would otherwise drift the number.
  const enrichment = useEnrichedAnomalies({
    windowStart,
    windowEnd,
    threshold,
    enabled: rows.length > 0,
  });
  const anomalyTotal = enrichment.anomalyCount ?? anomalies.length;

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
              // tables below filter to the same range.
              onRangeSelect={(from, to) => void setRange({ from, to })}
            />
          </Card>
          <Card title="Top anomalies">
            {/* Server-ranked rows once loaded — chart rows are downsampled and
                can show anomalies the enrichment payload doesn't cover. */}
            <AnomalyTable
              anomalies={enrichment.topRows ?? anomalies}
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
