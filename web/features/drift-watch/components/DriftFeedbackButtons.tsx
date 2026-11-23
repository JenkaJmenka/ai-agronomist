"use client";

import { Button } from "@/components/ui/Button";
import type { DriftWatchEpisode, DriftWatchLabel } from "@/features/drift-watch/api/driftWatch";
import { useDriftWatchFeedback } from "@/features/drift-watch/hooks/useDriftWatch";
import { useScope } from "@/lib/useScope";

const LABEL_TEXT: Record<string, string> = {
  confirmed_drift: "Confirmed drift",
  false_alarm: "False alarm",
};

export function DriftFeedbackButtons({ episode }: { readonly episode: DriftWatchEpisode }) {
  const { tenant, gh } = useScope();
  const feedback = useDriftWatchFeedback();
  const direction = episode.direction === "-" ? "-" : "+";

  const submit = (label: DriftWatchLabel) =>
    feedback.mutate({
      tenant_id: tenant,
      greenhouse_id: gh,
      input_id: episode.input_id,
      direction,
      first_detected_ts: episode.first_detected_ts,
      label,
    });

  return (
    <div className="space-y-1">
      <div className="text-xs text-text-muted" data-testid="episode-label">
        {episode.label ? `Label: ${LABEL_TEXT[episode.label] ?? episode.label}` : "Not labelled"}
      </div>
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="secondary"
          data-testid="feedback-confirmed"
          disabled={feedback.isPending}
          onClick={() => submit("confirmed_drift")}
        >
          Confirmed drift
        </Button>
        <Button
          size="sm"
          variant="secondary"
          data-testid="feedback-false-alarm"
          disabled={feedback.isPending}
          onClick={() => submit("false_alarm")}
        >
          False alarm
        </Button>
      </div>
      {feedback.isError ? (
        <p role="alert" className="text-xs text-sev-critical" data-testid="feedback-error">
          Could not save label: {feedback.error.message}
        </p>
      ) : null}
    </div>
  );
}
