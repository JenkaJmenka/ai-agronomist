"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { parseAsBoolean, useQueryState } from "nuqs";

import { TwoStepConfirm } from "@/components/ui/TwoStepConfirm";
import { clearTenantData, removeLiveDemoData } from "@/features/operations/api/operations";

interface ClearDataPanelProps {
  readonly tenant: string;
  readonly gh: string;
}

/** Destructive wipes: live demo only, or full tenant/greenhouse clear. */
export function ClearDataPanel({ tenant, gh }: ClearDataPanelProps) {
  const queryClient = useQueryClient();
  const [, setLive] = useQueryState("live", parseAsBoolean.withDefault(false));

  const afterWipe = async () => {
    await setLive(false);
    await queryClient.invalidateQueries();
  };

  const clearAllMutation = useMutation<unknown, Error>({
    mutationFn: () => clearTenantData({ tenant, gh }),
    onSuccess: afterWipe,
  });

  const removeLiveMutation = useMutation<unknown, Error>({
    mutationFn: () => removeLiveDemoData({ tenant, gh }),
    onSuccess: afterWipe,
  });

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <p className="text-sm text-text-muted">
          Removes Go Live / mock SCADA readings only. Client CSV uploads and inject-test anomalies
          stay. Live feed is muted until you turn Live back on.
        </p>
        <TwoStepConfirm
          triggerLabel="Remove live demo data"
          confirmLabel="Yes, remove live data"
          warning={
            <>
              Remove live/mock readings for{" "}
              <span className="font-mono">
                {tenant} / {gh}
              </span>
              ? Uploaded client data and inject-test rows are kept.
            </>
          }
          onConfirm={() => removeLiveMutation.mutate()}
          isBusy={removeLiveMutation.isPending}
          testId="remove-live-data"
        />
        {removeLiveMutation.error ? (
          <p role="alert" className="text-sm text-sev-critical">
            Remove live failed: {removeLiveMutation.error.message}
          </p>
        ) : null}
        {removeLiveMutation.isSuccess ? (
          <p role="status" className="text-sm text-sev-normal">
            Live demo data removed for {tenant} / {gh}. Live feed muted until Live is turned on.
          </p>
        ) : null}
      </div>

      <div className="space-y-3 border-t border-border pt-4">
        <p className="text-sm text-sev-warning">
          This permanently deletes all sensor readings, synthetic inject rows, and related incidents
          for the selected tenant and greenhouse. Live mock data stays muted until you turn Live
          back on.
        </p>
        <TwoStepConfirm
          triggerLabel="Clear all data"
          confirmLabel="Yes, delete everything"
          warning={
            <>
              Are you sure? This will permanently delete ALL data for{" "}
              <span className="font-mono">
                {tenant} / {gh}
              </span>
              . This cannot be undone.
            </>
          }
          onConfirm={() => clearAllMutation.mutate()}
          isBusy={clearAllMutation.isPending}
          testId="clear-data"
        />
        {clearAllMutation.error ? (
          <p role="alert" className="text-sm text-sev-critical">
            Clear failed: {clearAllMutation.error.message}
          </p>
        ) : null}
        {clearAllMutation.isSuccess ? (
          <p role="status" className="text-sm text-sev-normal">
            All data cleared for {tenant} / {gh}. Live feed muted until Live is turned on.
          </p>
        ) : null}
      </div>
    </div>
  );
}
