"use client";

import { Button } from "@/components/ui/Button";
import { Chip } from "@/components/ui/Chip";
import { Skeleton } from "@/components/ui/Skeleton";
import { TwoStepConfirm } from "@/components/ui/TwoStepConfirm";
import { useModelVersions } from "@/features/operations/hooks/useModelVersions";

interface ModelRegistryPanelProps {
  readonly tenant: string;
}

/** Tenant model versions with activate / delete (delete is two-step). */
export function ModelRegistryPanel({ tenant }: ModelRegistryPanelProps) {
  const {
    versions,
    activeVersion,
    isLoading,
    isError,
    error,
    activate,
    remove,
    isMutating,
    mutationError,
  } = useModelVersions(tenant);

  if (isLoading) {
    return <Skeleton className="h-24 w-full" />;
  }
  if (isError) {
    return (
      <p role="alert" className="text-sm text-sev-critical">
        Could not load models: {error?.message}
      </p>
    );
  }
  if (versions.length === 0) {
    return (
      <p className="text-sm text-text-muted">
        No trained models for this tenant — the next cold-run upload will create one.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {activeVersion ? (
        <p className="text-sm">
          Active model: <span className="font-mono">{activeVersion.model_id}</span> (
          {activeVersion.model_type ?? "unknown"})
        </p>
      ) : (
        <p className="text-sm text-sev-warning">
          No active deployment — cold-run will use the global baseline.
        </p>
      )}

      <ul className="divide-y divide-border">
        {versions.map((version) => (
          <li
            key={version.model_id}
            className="flex flex-wrap items-center justify-between gap-3 py-2"
          >
            <div>
              <p className="text-sm">
                <span className="font-mono">{version.model_id}</span> ·{" "}
                {version.model_type ?? "unknown"}{" "}
                {version.is_active ? <Chip severity="normal">active</Chip> : null}
              </p>
              <p className="text-xs text-text-muted tabular-nums">
                Created {(version.created_at ?? "").slice(0, 19) || "—"}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {!version.is_active ? (
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={isMutating}
                  onClick={() => activate(version.model_id)}
                  data-testid={`activate-${version.model_id}`}
                >
                  Activate
                </Button>
              ) : null}
              <TwoStepConfirm
                triggerLabel="Delete"
                confirmLabel="Confirm delete"
                warning={`Delete model ${version.model_id}? This cannot be undone.`}
                onConfirm={() => remove(version.model_id)}
                isBusy={isMutating}
                testId={`delete-${version.model_id}`}
              />
            </div>
          </li>
        ))}
      </ul>

      {mutationError ? (
        <p role="alert" className="text-sm text-sev-critical">
          Model operation failed: {mutationError.message}
        </p>
      ) : null}
    </div>
  );
}
