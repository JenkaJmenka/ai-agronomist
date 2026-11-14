"use client";

import { Skeleton } from "@/components/ui/Skeleton";
import { useLearningProgress } from "@/features/feedback/hooks/useLearningProgress";

interface LearningProgressCardProps {
  readonly tenant: string;
}

/** Progress toward the next retrain trigger (plan §6.2). */
export function LearningProgressCard({ tenant }: LearningProgressCardProps) {
  const { progress, isLoading, isError } = useLearningProgress(tenant);

  if (isLoading) {
    return <Skeleton className="h-20 w-full" />;
  }
  if (isError || !progress) {
    return (
      <p className="text-sm text-text-muted">
        Learning progress unavailable — feedback still counts toward retraining.
      </p>
    );
  }

  const percent = progress.progress_percent ?? 0;
  // API-sourced percentage isn't guaranteed to stay within 0-100; clamp
  // before it reaches the bar's width/aria-valuenow.
  const percentClamped = Math.min(100, Math.max(0, percent));
  return (
    <div className="space-y-2" data-testid="learning-progress">
      {/* ARIA progressbar, not <progress>: Chromium resolves accent-color
          on <progress> in computed style but doesn't paint it (verified by
          pixel-sampling), so the fill stays UA green regardless of theme.
          This div pair keeps the same semantics via ARIA while painting the
          fill ourselves. */}
      <div
        role="progressbar"
        aria-valuenow={Math.round(percentClamped)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Retrain progress"
        className="h-2 w-full overflow-hidden rounded-full bg-border-strong"
      >
        <div className="h-full rounded-full bg-accent" style={{ width: `${percentClamped}%` }} />
      </div>
      <p className="text-sm text-text-muted tabular-nums">
        {percentClamped.toFixed(0)}% toward next retrain · {progress.high_quality_count ?? 0}{" "}
        high-quality labels · {progress.samples_needed ?? 0} more needed
        {progress.last_retrain ? ` · last retrain ${progress.last_retrain.slice(0, 10)}` : ""}
      </p>
    </div>
  );
}
