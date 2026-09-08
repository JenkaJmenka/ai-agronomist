"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { Chip } from "@/components/ui/Chip";
import type { SourceStatus } from "@/features/monitoring/api/live";
import { LiveBadge } from "@/features/monitoring/components/LiveBadge";
import type { LiveTransport } from "@/features/monitoring/hooks/useLiveReadings";
import { pickDashboardSearchParams } from "@/lib/dashboardQuery";

interface HomeStatusBarProps {
  readonly transport: LiveTransport;
  readonly stale: boolean;
  readonly sourceStatuses: SourceStatus[];
  readonly anomalyCount: number | null;
  readonly critical: number;
  readonly warning: number;
  readonly lstm: number;
  readonly isCountLoading: boolean;
}

export function HomeStatusBar({
  transport,
  stale,
  sourceStatuses,
  anomalyCount,
  critical,
  warning,
  lstm,
  isCountLoading,
}: HomeStatusBarProps) {
  const search = useSearchParams();
  const anomaliesHref = `/anomalies?${pickDashboardSearchParams(search).toString()}`;

  return (
    <div
      className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-card)] border border-border bg-bg-elevated px-4 py-3"
      data-testid="home-status-bar"
    >
      <div className="flex flex-wrap items-center gap-2">
        <LiveBadge transport={transport} stale={stale} sourceStatuses={sourceStatuses} />
        {transport === "idle" ? (
          <span className="text-xs text-text-muted" data-testid="home-status-historical">
            Historical window
          </span>
        ) : null}
        <Link
          href={anomaliesHref}
          className="text-sm font-medium text-accent underline-offset-2 hover:underline"
          data-testid="home-anomaly-count-link"
        >
          {isCountLoading || anomalyCount === null
            ? "Anomalies…"
            : `${anomalyCount.toLocaleString()} anomalies in window`}
        </Link>
      </div>
      <div className="flex flex-wrap gap-1.5" data-testid="home-severity-summary">
        {critical > 0 ? <Chip severity="critical">{critical} critical</Chip> : null}
        {lstm > 0 ? <Chip severity="lstm">{lstm} LSTM / drift</Chip> : null}
        {warning > 0 ? <Chip severity="warning">{warning} warning</Chip> : null}
        {critical === 0 && lstm === 0 && warning === 0 ? (
          <Chip severity="normal">No open severity flags</Chip>
        ) : null}
      </div>
    </div>
  );
}
