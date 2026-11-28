"use client";

import { Chip } from "@/components/ui/Chip";
import type { DriftWatchInput } from "@/features/drift-watch/api/driftWatch";
import { DriftInputChartPanel } from "@/features/drift-watch/components/DriftInputChartPanel";
import type { Severity } from "@/lib/severity";

const KIND_LABELS: Record<string, string> = {
  paired_probes: "Paired probes",
  virtual_sensor: "Predicted from other sensors",
  physics: "Physics consistency",
};

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

const STATUS_SEVERITY: Record<string, Severity> = {
  armed: "normal",
  shadow: "unclassified",
  derived: "unclassified",
  uncalibrated: "warning",
};

function statusSeverity(status: string): Severity {
  return STATUS_SEVERITY[status] ?? "unclassified";
}

const ALERT_STATE_SEVERITY: Record<string, Severity> = {
  off: "unclassified",
  eligible: "normal",
  enabled: "normal",
  suspended: "warning",
};

function alertStateSeverity(state: string): Severity {
  return ALERT_STATE_SEVERITY[state] ?? "unclassified";
}

export function DriftInputRow({ input }: { readonly input: DriftWatchInput }) {
  const far =
    input.false_alarm_rate != null
      ? `${input.false_alarm_rate.toFixed(2)}/mo false alarms (calibration)`
      : null;
  const dr =
    input.detection_rate != null ? `${(input.detection_rate * 100).toFixed(0)}% detection` : null;
  const oos =
    input.oos_count != null && input.budget != null
      ? `${input.oos_count} of ${input.budget} episodes in 13 weeks (budget ${input.budget})`
      : null;

  return (
    <div
      data-testid="drift-input-row"
      className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 border-b border-border py-3 last:border-0"
    >
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-text" data-testid="input-channel">
            {input.target_channel}
          </span>
          <span className="text-sm text-text-muted">{kindLabel(input.kind)}</span>
          <span data-testid="input-status-chip">
            <Chip severity={statusSeverity(input.status)}>{input.status}</Chip>
          </span>
          {input.alert_state ? (
            <span data-testid="input-alert-state">
              <Chip severity={alertStateSeverity(input.alert_state)}>
                {`alerts: ${input.alert_state}`}
              </Chip>
            </span>
          ) : null}
        </div>
        {input.status_reason ? (
          <p className="mt-0.5 text-sm text-text-muted" data-testid="input-status-reason">
            {input.status_reason}
          </p>
        ) : null}
        {input.alert_state_reason ? (
          <p className="mt-0.5 text-sm text-text-muted" data-testid="input-alert-reason">
            {input.alert_state_reason}
          </p>
        ) : null}
      </div>
      {(oos ?? far ?? dr) ? (
        <div className="shrink-0 text-right text-sm text-text-muted tabular-nums">
          {oos ? <div data-testid="input-oos">{oos}</div> : null}
          {far ? <div data-testid="input-calibration-far">{far}</div> : null}
          {dr ? <div>{dr}</div> : null}
        </div>
      ) : null}
      <DriftInputChartPanel inputId={input.input_id} />
    </div>
  );
}
