"use client";

import { useQuery } from "@tanstack/react-query";
import { Suspense } from "react";

import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { STALE_TIME_MS } from "@/lib/query";

interface MetricsResponse {
  readonly period_hours: number;
  readonly total_detections: number;
  readonly false_positives: number;
  readonly true_positives: number;
  readonly false_positive_rate: number;
  readonly avg_confidence: number;
  readonly pattern_distribution: Record<string, number>;
  readonly confidence_distribution: Record<string, number>;
}

async function fetchMetrics(hours: number): Promise<MetricsResponse> {
  const res = await fetch(`/api/bff/monitoring/metrics?hours=${hours}`);
  if (!res.ok) throw new Error(`Metrics request failed (${res.status})`);
  return (await res.json()) as MetricsResponse;
}

/** Island 1: detection counters — refreshes every 30 s (plan §4.3). */
function DetectionCounters({ hours }: { readonly hours: number }) {
  const { data, isPending, isError } = useQuery({
    queryKey: ["metrics", "counters", hours],
    queryFn: () => fetchMetrics(hours),
    staleTime: STALE_TIME_MS.pipelineMetrics,
    refetchInterval: STALE_TIME_MS.pipelineMetrics,
  });

  if (isPending) return <Skeleton className="h-24 w-full" />;
  if (isError || !data) {
    return <p className="text-sm text-text-muted">Metrics unavailable — is the API reachable?</p>;
  }

  const cells = [
    { label: "Total detections", value: data.total_detections },
    { label: "True positives", value: data.true_positives },
    { label: "False positives", value: data.false_positives },
    { label: "FP rate", value: `${(data.false_positive_rate * 100).toFixed(1)}%` },
    { label: "Avg confidence", value: data.avg_confidence.toFixed(3) },
  ];
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
      {cells.map((cell) => (
        <div key={cell.label}>
          <p className="text-xs uppercase tracking-wide text-text-muted">{cell.label}</p>
          <p className="text-2xl font-bold tabular-nums">{cell.value}</p>
        </div>
      ))}
    </div>
  );
}

/** Island 2: pattern distribution — independent query, same cadence. */
function PatternDistribution({ hours }: { readonly hours: number }) {
  const { data, isPending, isError } = useQuery({
    queryKey: ["metrics", "patterns", hours],
    queryFn: () => fetchMetrics(hours),
    staleTime: STALE_TIME_MS.pipelineMetrics,
    refetchInterval: STALE_TIME_MS.pipelineMetrics,
  });

  if (isPending) return <Skeleton className="h-24 w-full" />;
  if (isError || !data) {
    return <p className="text-sm text-text-muted">Metrics unavailable.</p>;
  }

  const entries = Object.entries(data.pattern_distribution);
  if (entries.length === 0) {
    return (
      <p className="py-4 text-center text-sm text-text-muted">
        No pattern detections in the last {data.period_hours} h.
      </p>
    );
  }

  const max = Math.max(...entries.map(([, count]) => count));
  return (
    <ul className="space-y-2">
      {entries
        .sort(([, a], [, b]) => b - a)
        .map(([pattern, count]) => (
          <li key={pattern} className="flex items-center gap-3 text-sm">
            <span className="w-40 truncate">{pattern}</span>
            <span
              className="h-3 rounded-sm bg-accent/70"
              style={{ width: `${(count / max) * 60}%` }}
              aria-hidden="true"
            />
            <span className="tabular-nums text-text-muted">{count}</span>
          </li>
        ))}
    </ul>
  );
}

export function MetricsView() {
  const hours = 24;
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">System Metrics</h1>
        <p className="text-sm text-text-muted">
          Pipeline observability — auto-refreshes every 30 s.
        </p>
      </div>
      <Suspense fallback={<Skeleton className="h-24 w-full" />}>
        <Card title={`Detections (last ${hours} h)`}>
          <DetectionCounters hours={hours} />
        </Card>
        <div className="mt-6">
          <Card title="Pattern distribution">
            <PatternDistribution hours={hours} />
          </Card>
        </div>
      </Suspense>
    </div>
  );
}
