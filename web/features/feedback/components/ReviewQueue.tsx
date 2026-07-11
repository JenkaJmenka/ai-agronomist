"use client";

import { useState } from "react";

import { Chip } from "@/components/ui/Chip";
import type { OpenIncident, VerdictInput } from "@/features/feedback/api/feedback";
import { VerdictForm } from "@/features/feedback/components/VerdictForm";
import { FAULT_LABELS, getSeverity } from "@/lib/severity";

const VERDICT_THRESHOLD_HINT = 0.15;

interface ReviewQueueProps {
  readonly incidents: OpenIncident[];
  readonly onSubmit: (input: Omit<VerdictInput, "tenant">) => void;
  readonly isSubmitting: boolean;
}

function describeIncident(incident: OpenIncident): string {
  const details = incident.root_cause_details ?? {};
  const summary = details.hint_summary || details.reasoning;
  if (summary) return summary.slice(0, 120);
  const sensors = (details.affected_sensors ?? []).join(", ");
  const pattern = details.pattern_type || incident.root_cause || "";
  if (pattern && sensors) return `${pattern.replaceAll("_", " ")} on ${sensors}`;
  return "—";
}

/** Open incidents with an expandable verdict form per row. */
export function ReviewQueue({ incidents, onSubmit, isSubmitting }: ReviewQueueProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  if (incidents.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-text-muted">
        No open incidents to review. Incidents are created when comprehensive inference runs on
        uploaded data.
      </p>
    );
  }

  return (
    <ul className="space-y-3" data-testid="review-queue">
      {incidents.map((incident) => {
        const score = incident.anomaly_score ?? 0;
        const isSelected = selectedId === incident.id;
        return (
          <li
            key={incident.id}
            data-testid={`incident-${incident.id}`}
            className="rounded-[var(--radius-card)] border border-border bg-bg-elevated p-4"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm">
                  <span className="tabular-nums">
                    {(incident.detected_at ?? "").replace("T", " ").slice(0, 19) || "—"}
                  </span>{" "}
                  <Chip severity={getSeverity({ score, threshold: VERDICT_THRESHOLD_HINT })}>
                    {score.toFixed(3)}
                  </Chip>{" "}
                  {incident.fault_type ? (
                    <span className="text-text-muted">
                      {FAULT_LABELS[incident.fault_type] ?? incident.fault_type}
                    </span>
                  ) : null}
                </p>
                <p className="mt-1 text-sm text-text-muted">{describeIncident(incident)}</p>
              </div>
              <button
                type="button"
                onClick={() => setSelectedId(isSelected ? null : incident.id)}
                aria-expanded={isSelected}
                data-testid={`label-${incident.id}`}
                className="rounded-[var(--radius-control)] border border-border px-3 py-1.5 text-sm hover:border-accent"
              >
                {isSelected ? "Close" : "Label incident"}
              </button>
            </div>
            {isSelected ? (
              <div className="mt-3 border-t border-border pt-3">
                <VerdictForm
                  incidentId={incident.id}
                  onSubmit={onSubmit}
                  isSubmitting={isSubmitting}
                />
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
