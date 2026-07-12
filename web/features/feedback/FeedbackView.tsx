"use client";

import { Suspense } from "react";

import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { LearningProgressCard } from "@/features/feedback/components/LearningProgressCard";
import { ReviewQueue } from "@/features/feedback/components/ReviewQueue";
import { useReviewQueue } from "@/features/feedback/hooks/useReviewQueue";
import { useScope } from "@/lib/useScope";

function FeedbackBody() {
  const { tenant, gh } = useScope();
  const { incidents, isLoading, isError, error, submit, isSubmitting, submitError, lastVerdict } =
    useReviewQueue(tenant, gh);

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Operator Feedback</h1>
        <p className="text-sm text-text-muted tabular-nums">
          {tenant} · {gh} · review open incidents, label verdicts, track learning
        </p>
      </div>

      <Card title="Learning progress">
        <LearningProgressCard tenant={tenant} />
      </Card>

      {submitError ? (
        <p role="alert" className="text-sm text-sev-critical">
          Verdict failed and was rolled back: {submitError.message}
        </p>
      ) : null}
      {lastVerdict ? (
        <p role="status" className="text-sm text-sev-normal" data-testid="verdict-result">
          Verdict recorded ({lastVerdict.status}) · {lastVerdict.progress_to_retrain} toward retrain
          {lastVerdict.learning_triggered ? " · retraining triggered" : ""}
        </p>
      ) : null}

      {isLoading ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : isError ? (
        <Card>
          <p role="alert" className="py-4 text-center text-sm text-sev-critical">
            Could not load incidents: {error?.message}
          </p>
        </Card>
      ) : (
        <Card title="Review queue">
          <ReviewQueue incidents={incidents} onSubmit={submit} isSubmitting={isSubmitting} />
        </Card>
      )}
    </div>
  );
}

export function FeedbackView() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-24 w-full" />
        </div>
      }
    >
      <FeedbackBody />
    </Suspense>
  );
}
