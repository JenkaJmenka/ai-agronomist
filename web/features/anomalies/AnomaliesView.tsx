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
import { SetpointExcursionsSection } from "@/features/anomalies/components/SetpointExcursionsSection";
import { ThresholdControl } from "@/features/anomalies/components/ThresholdControl";
import { useAnomalies } from "@/features/anomalies/hooks/useAnomalies";
import { useAnomalyCount } from "@/features/anomalies/hooks/useAnomalyCount";
import { useEnrichedAnomalies } from "@/features/anomalies/hooks/useEnrichedAnomalies";
import { collapseSetpointExcursions } from "@/features/anomalies/lib/setpointExcursions";
import { discoverChannels, toDisplayChannel } from "@/features/monitoring/api/readings";
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
  // Cheap exact count (G2) — the one authoritative anomaly total for the
  // page. G4's anomaly_count is also exact but is fetched alongside a
  // 40+ second row payload in production and, on error, previously fell
  // back to the downsampled chart row count with no indication it was an
  // estimate. This is faster, independent of /episodes and /enriched, and
  // reused below so the episode panel's count can never drift from the
  // header's.
  const anomalyCountState = useAnomalyCount({
    windowStart,
    windowEnd,
    threshold,
    enabled: rows.length > 0 && !isRefetching,
  });
  const anomalyTotal = anomalyCountState.count;
  const tableAnomalies = enrichment.topRows ?? anomalies;

  // Build the set of channels with detector anomalies in this window (from
  // enrichment affected_sensors), for the setpoint excursion cross-reference.
  const anomalyChannels = useMemo(() => {
    const chSet = new Set<string>();
    for (const detail of enrichment.byTimestamp.values()) {
      for (const sensor of detail.affectedSensors) {
        chSet.add(toDisplayChannel(sensor));
      }
    }
    return chSet;
  }, [enrichment.byTimestamp]);

  // Collapse contiguous out-of-range readings into spans for the excursion
  // section (computed only once readings are settled).
  const excursionSpans = useMemo(
    () =>
      rows.length > 0 && !isRefetching
        ? collapseSetpointExcursions(rows, channels, setpoints, anomalyChannels)
        : [],
    [rows, channels, setpoints, anomalyChannels, isRefetching],
  );

  // Dim + debounced URL write for chart drag-select / scroll-zoom.
  const { isDimmed, onChartRangeSelect } = useChartRangeSelect({ isReadingsBusy: isRefetching });
  const { chartViewWindow, onTableRowSelect } = useChartViewWindow({ from, to });

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
          <p className="text-sm tabular-nums" data-testid="anomaly-count-summary">
            {anomalyTotal !== null ? (
              <span className="font-semibold">{anomalyTotal.toLocaleString()}</span>
            ) : (
              <span className="font-semibold text-text-muted">—</span>
            )}
            <span className="text-text-muted"> anomalies ≥ {threshold.toFixed(3)} (exact) in </span>
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
                setpoints={setpoints}
                onWindowSelect={onTableRowSelect}
                anomalyCount={anomalyTotal}
                fallback={
                  <AnomalyTable
                    mode="top"
                    anomalies={tableAnomalies}
                    enrichment={enrichment.byTimestamp}
                    isEnrichmentLoading={enrichment.isLoading}
                    onWindowSelect={onTableRowSelect}
                    setpoints={setpoints}
                    readings={rows}
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
                setpoints={setpoints}
                readings={rows}
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
                setpoints={setpoints}
                readings={rows}
              />
            </Card>
          ) : null}
          <Card>
            <SetpointExcursionsSection spans={excursionSpans} />
          </Card>
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
                setpoints={setpoints}
                anomalyCount={anomalyTotal}
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
          <Card>
            <SetpointExcursionsSection spans={excursionSpans} />
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
