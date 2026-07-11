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
  return (
    <div className="space-y-2" data-testid="learning-progress">
      <progress value={percent} max={100} className="h-2 w-full" aria-label="Retrain progress" />
      <p className="text-sm text-text-muted tabular-nums">
        {percent.toFixed(0)}% toward next retrain · {progress.high_quality_count ?? 0} high-quality
        labels · {progress.samples_needed ?? 0} more needed
        {progress.last_retrain ? ` · last retrain ${progress.last_retrain.slice(0, 10)}` : ""}
      </p>
    </div>
  );
}
