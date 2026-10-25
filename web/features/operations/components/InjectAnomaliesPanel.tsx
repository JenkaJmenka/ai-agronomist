"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { useReadingsBounds } from "@/features/monitoring/hooks/useReadingsBounds";
import {
  INJECT_ANOMALY_TYPES,
  INJECT_TYPE_LABELS,
  type InjectAnomalyType,
  type InjectResult,
  injectTestAnomalies,
  type RemoveResult,
  removeTestAnomalies,
} from "@/features/operations/api/operations";
import { resolveInjectWindow } from "@/features/operations/lib/injectWindow";
import { formatTimestamp } from "@/lib/formatTime";

interface InjectAnomaliesPanelProps {
  readonly tenant: string;
  readonly gh: string;
}

/** Inject / remove tagged synthetic anomaly readings (demo & QA tooling). */
export function InjectAnomaliesPanel({ tenant, gh }: InjectAnomaliesPanelProps) {
  const [anomalyType, setAnomalyType] = useState<InjectAnomalyType>("random");
  const [anomalyCount, setAnomalyCount] = useState(8);
  const [normalCount, setNormalCount] = useState(96);
  const [removeType, setRemoveType] = useState<InjectAnomalyType | "">("");

  const { maxTimestamp } = useReadingsBounds({ tenant, gh });

  const injectMutation = useMutation<InjectResult, Error>({
    mutationFn: () => {
      const { windowStart, windowEnd } = resolveInjectWindow({
        maxTimestamp: maxTimestamp ? new Date(maxTimestamp) : null,
      });
      return injectTestAnomalies({
        tenant,
        gh,
        anomalyType,
        anomalyCount,
        normalCount,
        windowStart,
        windowEnd,
      });
    },
  });
  const removeMutation = useMutation<RemoveResult, Error>({
    mutationFn: () => removeTestAnomalies({ tenant, gh, anomalyType: removeType || undefined }),
  });

  return (
    <div className="space-y-4">
      <p className="text-sm text-text-muted">
        Generates synthetic readings with pre-scored anomaly spikes. Injected rows are tagged so
        they can be removed without touching real sensor data.
      </p>

      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">
          <span className="font-medium">Anomaly type</span>
          <select
            className="mt-1 block rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm"
            value={anomalyType}
            onChange={(e) => setAnomalyType(e.target.value as InjectAnomalyType)}
            data-testid="inject-type"
          >
            {INJECT_ANOMALY_TYPES.map((value) => (
              <option key={value} value={value}>
                {INJECT_TYPE_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="font-medium">Anomaly count</span>
          <input
            type="number"
            min={1}
            max={50}
            value={anomalyCount}
            onChange={(e) => setAnomalyCount(Number(e.target.value))}
            className="mt-1 block w-24 rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm tabular-nums"
          />
        </label>
        <label className="text-sm">
          <span className="font-medium">Background readings</span>
          <input
            type="number"
            min={0}
            max={500}
            step={10}
            value={normalCount}
            onChange={(e) => setNormalCount(Number(e.target.value))}
            className="mt-1 block w-24 rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm tabular-nums"
          />
        </label>
        <Button
          onClick={() => injectMutation.mutate()}
          disabled={injectMutation.isPending}
          data-testid="inject-submit"
        >
          {injectMutation.isPending ? "Injecting…" : "Inject anomalies"}
        </Button>
      </div>

      {injectMutation.error ? (
        <p role="alert" className="text-sm text-sev-critical">
          Injection failed: {injectMutation.error.message}
        </p>
      ) : null}
      {injectMutation.data ? (
        <p role="status" className="text-sm text-sev-normal tabular-nums">
          Injected {injectMutation.data.injected} readings ({injectMutation.data.anomalies} ×{" "}
          {INJECT_TYPE_LABELS[anomalyType]}) for {tenant}/{gh}, window{" "}
          {formatTimestamp(injectMutation.data.window_start)} to{" "}
          {formatTimestamp(injectMutation.data.window_end)}.
        </p>
      ) : null}

      <div className="flex flex-wrap items-end gap-3 border-t border-border pt-3">
        <label className="text-sm">
          <span className="font-medium">Remove synthetic rows</span>
          <select
            className="mt-1 block rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm"
            value={removeType}
            onChange={(e) => setRemoveType(e.target.value as InjectAnomalyType | "")}
            data-testid="remove-type"
          >
            <option value="">All synthetic anomalies</option>
            {INJECT_ANOMALY_TYPES.map((value) => (
              <option key={value} value={value}>
                {INJECT_TYPE_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <Button
          variant="secondary"
          onClick={() => removeMutation.mutate()}
          disabled={removeMutation.isPending}
          data-testid="remove-submit"
        >
          {removeMutation.isPending ? "Removing…" : "Remove"}
        </Button>
      </div>

      {removeMutation.error ? (
        <p role="alert" className="text-sm text-sev-critical">
          Remove failed: {removeMutation.error.message}
        </p>
      ) : null}
      {removeMutation.data ? (
        <p role="status" className="text-sm text-sev-normal tabular-nums">
          Removed {removeMutation.data.deleted_sensor} sensor readings and{" "}
          {removeMutation.data.deleted_raw} raw readings (type: {removeMutation.data.anomaly_type}).
        </p>
      ) : null}
    </div>
  );
}
