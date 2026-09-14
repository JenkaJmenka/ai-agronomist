"use client";

import { Suspense, useMemo, useState } from "react";

import { Card } from "@/components/ui/Card";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { SERVER_GROUPING_THRESHOLD } from "@/features/anomalies/api/flaggedWindow";
import { AnomalyTable } from "@/features/anomalies/components/AnomalyTable";
import { EpisodeTable } from "@/features/anomalies/components/EpisodeTable";
import { GroupedAnomalies } from "@/features/anomalies/components/GroupedAnomalies";
import { ScoreTimeline } from "@/features/anomalies/components/ScoreTimeline";
import { ThresholdControl } from "@/features/anomalies/components/ThresholdControl";
import { useAnomalies } from "@/features/anomalies/hooks/useAnomalies";
import { useEnrichedAnomalies } from "@/features/anomalies/hooks/useEnrichedAnomalies";
import { discoverChannels } from "@/features/monitoring/api/readings";
import { useSetpoints } from "@/features/monitoring/hooks/useSetpoints";
import { SharedViewControls } from "@/features/shell/SharedViewControls";
import { useChartRangeSelect } from "@/lib/useChartRangeSelect";
import { useChartViewWindow } from "@/lib/useChartViewWindow";
import { useDateRange } from "@/lib/useDateRange";
import { useScope } from "@/lib/useScope";

type ViewMode = "graphical" | "table";

/** Table view sub-mode. Episodes lead: one row per event, not per 5-min sample. */
type TableMode = "episodes" | "grouped";

const TABLE_MODE_LABELS: Readonly<Record<TableMode, string>> = {
  episodes: "Episodes",
  grouped: "Grouped",
};

/** Graphical view anomaly list. Episodes lead here too — this is the landing view. */
type ChartMode = "episodes" | "samples";

const CHART_MODE_LABELS: Readonly<Record<ChartMode, string>> = {
  episodes: "Episodes",
  samples: "Samples",
};

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
  const [tableMode, setTableMode] = useState<TableMode>("episodes");
  const [chartMode, setChartMode] = useState<ChartMode>("episodes");
  const [{ from, to }] = useDateRange();
  const { tenant, gh } = useScope();
  const { setpoints, updateSetpoint, resetToDefaults } = useSetpoints();
  const channels = useMemo(() => discoverChannels(rows), [rows]);

  // Envelope bounds drive the G4/G3 queries — rows are timestamp-ordered.
  const windowStart = useMemo(() => rows[0]?.timestamp ?? null, [rows]);
  const windowEnd = useMemo(() => rows.at(-1)?.timestamp ?? null, [rows]);
  // Defer enrich until readings settle so brush-zoom is not queued behind G4.
  const enrichment = useEnrichedAnomalies({
    windowStart,
    windowEnd,
    threshold,
    enabled: rows.length > 0 && !isRefetching,
    limit: SERVER_GROUPING_THRESHOLD,
  });

  // Dim + debounced URL write for chart drag-select / scroll-zoom.
  const { isDimmed, onChartRangeSelect } = useChartRangeSelect({ isReadingsBusy: isRefetching });
  const { chartViewWindow, onTableRowSelect } = useChartViewWindow({ from, to });
  // Use the exact count from the enriched response once known; while the slow
  // G4 request is in flight show null (not the downsampled anomalies.length
  // which can be 35 instead of the true 2649, causing GroupedAnomalies to
  // pick the wrong routing path and render silently-blank enrichment cells).
  const anomalyTotal: number | null =
    enrichment.anomalyCount ?? (enrichment.isLoading ? null : anomalies.length);
  const tableAnomalies = enrichment.topRows ?? anomalies;

  // Scope key ties the chart baseline to this page + tenant + greenhouse.
  const scopeKey = `anomalies:${tenant}:${gh}`;

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
          pageId="anomalies"
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
            {anomalyTotal !== null ? (
              <span className="font-semibold">{anomalyTotal.toLocaleString()}</span>
            ) : (
              <span className="font-semibold text-text-muted">—</span>
            )}
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
        <RefetchingBlock isRefetching={isDimmed} className="space-y-6">
          <Card title="Anomaly score timeline">
            <ScoreTimeline
              rows={rows}
              threshold={threshold}
              // Drag-selecting a span narrows the URL window, so the anomaly
              // tables below filter to the same range — and the Period control.
              onRangeSelect={onChartRangeSelect}
              viewWindow={chartViewWindow}
              scopeKey={scopeKey}
            />
          </Card>
          <Card title={chartMode === "episodes" ? "Anomaly episodes" : "Top anomalies"}>
            <div
              className="mb-3 flex flex-wrap items-center gap-2 text-sm"
              data-testid="chart-anomaly-mode"
            >
              <span className="text-text-muted">Show</span>
              {(["episodes", "samples"] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={chartMode === mode}
                  onClick={() => setChartMode(mode)}
                  className={`rounded-[var(--radius-control)] border px-3 py-1 ${
                    chartMode === mode
                      ? "border-accent font-medium text-accent"
                      : "border-border text-text-muted hover:text-text"
                  }`}
                >
                  {CHART_MODE_LABELS[mode]}
                </button>
              ))}
            </div>
            {chartMode === "episodes" ? (
              <EpisodeTable
                windowStart={windowStart}
                windowEnd={windowEnd}
                threshold={threshold}
                onWindowSelect={onTableRowSelect}
                fallback={
                  <AnomalyTable
                    mode="top"
                    anomalies={tableAnomalies}
                    enrichment={enrichment.byTimestamp}
                    isEnrichmentLoading={enrichment.isLoading}
                    onWindowSelect={onTableRowSelect}
                  />
                }
              />
            ) : (
              <AnomalyTable
                mode="top"
                anomalies={tableAnomalies}
                enrichment={enrichment.byTimestamp}
                isEnrichmentLoading={enrichment.isLoading}
                onWindowSelect={onTableRowSelect}
              />
            )}
          </Card>
          {chartMode === "samples" ? (
            <Card
              title={
                anomalyTotal !== null && anomalyTotal > 0
                  ? `All anomalies (${anomalyTotal.toLocaleString()})`
                  : "All anomalies"
              }
            >
              <AnomalyTable
                mode="all"
                anomalies={tableAnomalies}
                enrichment={enrichment.byTimestamp}
                isEnrichmentLoading={enrichment.isLoading}
                onWindowSelect={onTableRowSelect}
              />
            </Card>
          ) : null}
        </RefetchingBlock>
      ) : (
        <RefetchingBlock isRefetching={isDimmed}>
          <Card
            title={tableMode === "episodes" ? "Anomaly episodes" : "Detected anomalies (grouped)"}
          >
            <div
              className="mb-3 flex flex-wrap items-center gap-2 text-sm"
              data-testid="table-mode"
            >
              <span className="text-text-muted">Show</span>
              {(["episodes", "grouped"] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={tableMode === mode}
                  onClick={() => setTableMode(mode)}
                  className={`rounded-[var(--radius-control)] border px-3 py-1 ${
                    tableMode === mode
                      ? "border-accent font-medium text-accent"
                      : "border-border text-text-muted hover:text-text"
                  }`}
                >
                  {TABLE_MODE_LABELS[mode]}
                </button>
              ))}
            </div>
            {tableMode === "episodes" ? (
              <EpisodeTable
                windowStart={windowStart}
                windowEnd={windowEnd}
                threshold={threshold}
                fallback={
                  <GroupedAnomalies
                    windowStart={windowStart}
                    windowEnd={windowEnd}
                    threshold={threshold}
                  />
                }
              />
            ) : (
              <GroupedAnomalies
                windowStart={windowStart}
                windowEnd={windowEnd}
                threshold={threshold}
              />
            )}
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
