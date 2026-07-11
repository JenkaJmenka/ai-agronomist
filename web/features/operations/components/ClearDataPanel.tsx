"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { TwoStepConfirm } from "@/components/ui/TwoStepConfirm";
import { clearTenantData } from "@/features/operations/api/operations";

interface ClearDataPanelProps {
  readonly tenant: string;
  readonly gh: string;
}

/** Destructive tenant/greenhouse wipe behind the two-step confirm. */
export function ClearDataPanel({ tenant, gh }: ClearDataPanelProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation<unknown, Error>({
    mutationFn: () => clearTenantData({ tenant, gh }),
    // Every cached read for this scope is stale after a wipe.
    onSuccess: () => void queryClient.invalidateQueries(),
  });

  return (
    <div className="space-y-3">
      <p className="text-sm text-sev-warning">
        This permanently deletes all sensor readings for the selected tenant and greenhouse.
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
        onConfirm={() => mutation.mutate()}
        isBusy={mutation.isPending}
        testId="clear-data"
      />
      {mutation.error ? (
        <p role="alert" className="text-sm text-sev-critical">
          Clear failed: {mutation.error.message}
        </p>
      ) : null}
      {mutation.isSuccess ? (
        <p role="status" className="text-sm text-sev-normal">
          All data cleared for {tenant} / {gh}.
        </p>
      ) : null}
    </div>
  );
}
