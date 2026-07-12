"use client";

import { type FormEvent, useState } from "react";

import { Button } from "@/components/ui/Button";
import type { VerdictInput } from "@/features/feedback/api/feedback";

const DEFAULT_CONFIDENCE = 0.85;

interface VerdictFormProps {
  readonly incidentId: string;
  readonly onSubmit: (input: Omit<VerdictInput, "tenant">) => void;
  readonly isSubmitting: boolean;
}

/** Operator verdict form — submission is optimistic in useReviewQueue. */
export function VerdictForm({ incidentId, onSubmit, isSubmitting }: VerdictFormProps) {
  const [isTrueAnomaly, setIsTrueAnomaly] = useState(true);
  const [operatorId, setOperatorId] = useState("operator_1");
  const [confidence, setConfidence] = useState(DEFAULT_CONFIDENCE);
  const [patternObserved, setPatternObserved] = useState("");
  const [actualRootCause, setActualRootCause] = useState("");

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!operatorId.trim()) return;
    onSubmit({
      incidentId,
      isTrueAnomaly,
      operatorId: operatorId.trim(),
      confidence,
      patternObserved: patternObserved.trim() || undefined,
      actualRootCause: actualRootCause.trim() || undefined,
    });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-3" data-testid="verdict-form">
      <fieldset className="flex gap-4 text-sm">
        <legend className="mb-1 font-medium">Verdict</legend>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="verdict"
            checked={isTrueAnomaly}
            onChange={() => setIsTrueAnomaly(true)}
          />
          True anomaly
        </label>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="verdict"
            checked={!isTrueAnomaly}
            onChange={() => setIsTrueAnomaly(false)}
          />
          False positive
        </label>
      </fieldset>

      <div className="flex flex-wrap gap-3">
        <label className="text-sm">
          <span className="font-medium">Operator ID</span>
          <input
            type="text"
            value={operatorId}
            onChange={(e) => setOperatorId(e.target.value)}
            required
            className="mt-1 block rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm"
          />
        </label>
        <label className="text-sm">
          <span className="font-medium">
            Confidence <span className="tabular-nums">{confidence.toFixed(2)}</span>
          </span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={confidence}
            onChange={(e) => setConfidence(Number(e.target.value))}
            className="mt-2 block w-40"
          />
        </label>
      </div>

      <div className="flex flex-wrap gap-3">
        <label className="text-sm">
          <span className="font-medium">Pattern observed (optional)</span>
          <input
            type="text"
            value={patternObserved}
            onChange={(e) => setPatternObserved(e.target.value)}
            className="mt-1 block rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm"
          />
        </label>
        <label className="text-sm">
          <span className="font-medium">Root cause (optional)</span>
          <input
            type="text"
            value={actualRootCause}
            onChange={(e) => setActualRootCause(e.target.value)}
            className="mt-1 block rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm"
          />
        </label>
      </div>

      <Button type="submit" disabled={isSubmitting} data-testid="verdict-submit">
        {isSubmitting ? "Submitting…" : "Submit verdict"}
      </Button>
    </form>
  );
}
