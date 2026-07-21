"use client";

import { parseAsFloat, useQueryState } from "nuqs";
import { Suspense, useMemo } from "react";

import { Card } from "@/components/ui/Card";
import { Chip } from "@/components/ui/Chip";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { ThresholdControl } from "@/features/anomalies/components/ThresholdControl";
import { discoverChannels } from "@/features/monitoring/api/readings";
import { useReadings } from "@/features/monitoring/hooks/useReadings";
import { useSetpoints } from "@/features/monitoring/hooks/useSetpoints";
import { SharedViewControls } from "@/features/shell/SharedViewControls";
import { DEFAULT_SCORE_THRESHOLD } from "@/lib/severity";

function countFlagged(scores: number[], cutoff: number): number {
  let count = 0;
  for (const score of scores) if (score >= cutoff) count += 1;
  return count;
}

function quantile(sorted: number[], q: number): number {
  if (sorted.length === 0) return 0;
  const pos = Math.min(Math.floor(sorted.length * q), sorted.length - 1);
  return sorted[pos];
}

function ThresholdsBody() {
  const { rows, isLoading, isRefetching, isError, error, windowLabel } = useReadings();
  // Same `threshold` URL key as Monitoring / Anomalies so the slider stays shared.
  const [threshold, setThreshold] = useQueryState(
    "threshold",
    parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD),
  );
  const [saved] = useQueryState("saved", parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD));
  const { setpoints, updateSetpoint, resetToDefaults } = useSetpoints();
  const channels = useMemo(() => discoverChannels(rows), [rows]);

  const scores = useMemo(() => {
    const out: number[] = [];
    for (const row of rows) {
      if (typeof row.anomaly_score === "number") out.push(row.anomaly_score);
    }
    return out.sort((a, b) => a - b);
  }, [rows]);

  if (isError) throw error ?? new Error("Failed to load scores");

  const flaggedSaved = countFlagged(scores, saved);
  const flaggedPreview = countFlagged(scores, threshold);
  const delta = flaggedPreview - flaggedSaved;

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Threshold Management</h1>
          <p className="text-sm text-text-muted tabular-nums">{windowLabel}</p>
        </div>
        <SharedViewControls
          channels={channels}
          setpoints={setpoints}
          onUpdateSetpoint={updateSetpoint}
          onResetSetpoints={resetToDefaults}
        />
      </div>

      {isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : scores.length === 0 ? (
        <Card>
          <p className="py-8 text-center text-sm text-text-muted">
            No scored readings in the selected window.
          </p>
        </Card>
      ) : (
        <RefetchingBlock isRefetching={isRefetching} className="space-y-6">
          <Card title="Threshold">
            <ThresholdControl threshold={threshold} onChange={(v) => void setThreshold(v)} />
            <p className="mt-3 text-sm text-text-muted tabular-nums">
              Score range in window: {scores[0].toFixed(3)} → {scores[scores.length - 1].toFixed(3)}{" "}
              · p90 {quantile(scores, 0.9).toFixed(3)} · p95 {quantile(scores, 0.95).toFixed(3)}
            </p>
          </Card>

          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Card title="Points in window">
              <p className="text-2xl font-bold tabular-nums">{rows.length.toLocaleString()}</p>
            </Card>
            <Card title="Points with scores">
              <p className="text-2xl font-bold tabular-nums">{scores.length.toLocaleString()}</p>
            </Card>
            <Card title={`Flagged at saved (${saved.toFixed(3)})`}>
              <p className="text-2xl font-bold tabular-nums">{flaggedSaved}</p>
            </Card>
            <Card title={`Flagged at threshold (${threshold.toFixed(3)})`}>
              <p className="text-2xl font-bold tabular-nums">{flaggedPreview}</p>
              <Chip severity={delta > 0 ? "warning" : "normal"}>
                {delta >= 0 ? `+${delta}` : delta} vs saved
              </Chip>
            </Card>
          </div>
        </RefetchingBlock>
      )}
    </div>
  );
}

export function ThresholdsView() {
  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <ThresholdsBody />
    </Suspense>
  );
}
