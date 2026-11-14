"use client";

import { Button } from "@/components/ui/Button";
import { Chip } from "@/components/ui/Chip";
import { isActiveJobStatus, type JobSummary } from "@/features/jobs/api/jobs";
import { useJobProgress } from "@/features/jobs/hooks/useJobProgress";
import { formatTimestamp } from "@/lib/formatTime";
import type { Severity } from "@/lib/severity";

interface JobCardProps {
  readonly job: JobSummary;
  readonly onTerminate: (jobId: string) => void;
  readonly isTerminating: boolean;
}

function statusSeverity(status: string): Severity {
  if (status === "completed") return "normal";
  if (status === "failed") return "critical";
  if (status === "partially_failed") return "warning";
  return "lstm"; // in-flight
}

/** One job row: live progress (SSE with polling fallback) + terminate. */
export function JobCard({ job, onTerminate, isTerminating }: JobCardProps) {
  const { progress, transport } = useJobProgress(job.id, job.status);

  const status = progress?.status ?? job.status;
  const completed = progress?.completed_portions ?? job.completed_portions;
  const total = progress?.total_portions ?? job.total_portions ?? 0;
  const pct = progress?.progress_pct ?? job.progress_pct ?? (status === "completed" ? 100 : 0);
  // API-sourced percentage isn't guaranteed to stay within 0-100; clamp
  // before it reaches the bar's width/aria-valuenow.
  const pctClamped = Math.min(100, Math.max(0, pct ?? 0));

  return (
    <li
      data-testid={`job-${job.id}`}
      className="space-y-2 rounded-[var(--radius-card)] border border-border bg-bg-elevated p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm">
          <span className="font-mono">{job.id}</span>{" "}
          <span className="text-text-muted">· {job.job_type}</span>
        </p>
        <div className="flex items-center gap-2">
          <Chip severity={statusSeverity(status)}>{status}</Chip>
          {isActiveJobStatus(status) ? (
            <Button
              variant="secondary"
              size="sm"
              disabled={isTerminating}
              onClick={() => onTerminate(job.id)}
              data-testid={`terminate-${job.id}`}
            >
              Terminate
            </Button>
          ) : null}
        </div>
      </div>

      {isActiveJobStatus(status) ? (
        <div className="space-y-1">
          {/* ARIA progressbar, not <progress>: Chromium resolves accent-color
              on <progress> in computed style but doesn't paint it (verified
              by pixel-sampling), so the fill stays UA green regardless of
              theme. This div pair keeps the same semantics via ARIA while
              painting the fill ourselves. */}
          <div
            role="progressbar"
            aria-valuenow={Math.round(pctClamped)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Job progress"
            className="h-2 w-full overflow-hidden rounded-full bg-border-strong"
          >
            <div className="h-full rounded-full bg-accent" style={{ width: `${pctClamped}%` }} />
          </div>
          <p className="text-xs text-text-muted tabular-nums">
            {total > 0
              ? `${completed}/${total} portions · ${pctClamped.toFixed(1)}%`
              : "Preparing (cold-start training can take a few minutes)…"}
            {transport === "polling" ? " · live stream unavailable, polling" : ""}
          </p>
        </div>
      ) : (
        <p className="text-xs text-text-muted tabular-nums">
          Created {formatTimestamp(job.created_at)} · Finished {formatTimestamp(job.finished_at)}
        </p>
      )}

      {job.failed_portions > 0 ? (
        <p role="alert" className="text-sm text-sev-critical tabular-nums">
          {job.failed_portions} portions failed during execution.
        </p>
      ) : null}
      {job.error ? (
        <p role="alert" className="text-sm text-sev-critical">
          Error: {job.error}
        </p>
      ) : null}
    </li>
  );
}
