"use client";

import { Suspense } from "react";

import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { TwoStepConfirm } from "@/components/ui/TwoStepConfirm";
import {
  Co2EnrichmentToggle,
  SeasonMapEditor,
  SetpointsEditor,
  useConfigDraft,
} from "@/features/configuration/components/ConfigEditors";
import { useOperationalConfig } from "@/features/configuration/hooks/useOperationalConfig";
import { useScope } from "@/lib/useScope";

function ConfigurationBody() {
  const { tenant, gh } = useScope();
  const { config, sources, isLoading, isError, error, cropProfiles, save } = useOperationalConfig(
    tenant,
    gh,
  );
  const { draft, setDraft, isDirty } = useConfigDraft(config);

  if (isError) throw error ?? new Error("Failed to load operational config");

  if (isLoading || !draft) {
    return (
      <div className="mx-auto max-w-6xl space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const cropNames = cropProfiles ? Object.keys(cropProfiles).sort() : [draft.crop];

  return (
    <div className="mx-auto max-w-6xl space-y-6" data-testid="configuration-page">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Greenhouse Profile</h1>
          <p className="text-sm text-text-muted tabular-nums">
            {tenant} · {gh} — crop setpoints &amp; seasonal mapping
          </p>
        </div>
        <TwoStepConfirm
          testId="config-save"
          triggerLabel={isDirty ? "Save changes" : "Saved"}
          confirmLabel="Confirm save"
          warning="Saving replaces operational setpoints and the season map for this greenhouse. Detector sensitivity will use the new baselines on the next evaluation."
          disabled={!isDirty || save.isPending}
          isBusy={save.isPending}
          triggerVariant="primary"
          onConfirm={() => {
            save.mutate(draft);
          }}
        />
      </div>

      {save.isError ? (
        <p className="text-sm text-sev-critical" role="alert">
          {save.error instanceof Error ? save.error.message : "Save failed"}
        </p>
      ) : null}
      {save.isSuccess && !isDirty ? (
        <p className="text-sm text-sev-ok" role="status">
          Greenhouse profile saved.
        </p>
      ) : null}

      <Card title="Crop profile">
        <div className="flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium text-text-muted">Active crop</span>
            <select
              className="rounded-[var(--radius-control)] border border-border bg-surface px-3 py-2"
              value={draft.crop}
              data-testid="crop-select"
              onChange={(event) => {
                const crop = event.target.value;
                const profile = cropProfiles?.[crop];
                setDraft((prev) =>
                  prev
                    ? {
                        ...prev,
                        crop,
                        setpoints: profile ? structuredClone(profile) : prev.setpoints,
                      }
                    : prev,
                );
              }}
            >
              {cropNames.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <p className="text-xs text-text-muted">
            Source: {sources.crop ?? "default"} · changing crop loads that profile&apos;s day/night
            bands (tomato / cucumber / sweet pepper / strawberry from the agronomic setpoint table).
          </p>
        </div>
      </Card>

      <Card title="Day / night window">
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium text-text-muted">Day starts (hour UTC)</span>
            <input
              type="number"
              min={0}
              max={23}
              className="w-24 rounded border border-border bg-surface px-2 py-1.5"
              value={draft.day_window.start_hour}
              onChange={(e) => {
                const start_hour = Math.min(23, Math.max(0, Number(e.target.value) || 0));
                setDraft((prev) =>
                  prev ? { ...prev, day_window: { ...prev.day_window, start_hour } } : prev,
                );
              }}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium text-text-muted">Day ends (hour UTC)</span>
            <input
              type="number"
              min={0}
              max={23}
              className="w-24 rounded border border-border bg-surface px-2 py-1.5"
              value={draft.day_window.end_hour}
              onChange={(e) => {
                const end_hour = Math.min(23, Math.max(0, Number(e.target.value) || 0));
                setDraft((prev) =>
                  prev ? { ...prev, day_window: { ...prev.day_window, end_hour } } : prev,
                );
              }}
            />
          </label>
        </div>
      </Card>

      <Card title="CO₂ enrichment">
        <Co2EnrichmentToggle
          value={draft.co2_enrichment}
          onChange={(co2_enrichment) =>
            setDraft((prev) => (prev ? { ...prev, co2_enrichment } : prev))
          }
        />
      </Card>

      <Card title="Product-specific setpoints">
        <SetpointsEditor
          setpoints={draft.setpoints}
          onChange={(setpoints) => setDraft((prev) => (prev ? { ...prev, setpoints } : prev))}
        />
      </Card>

      <Card title="Seasonal mapping">
        <p className="mb-3 text-sm text-text-muted">
          Map each calendar month to Summer or Winter. Seasonal statistical baselines (mean, σ,
          median) are computed from historical telemetry in the mapped months.
        </p>
        <SeasonMapEditor
          seasonMap={draft.season_map}
          onChange={(season_map) => setDraft((prev) => (prev ? { ...prev, season_map } : prev))}
        />
      </Card>
    </div>
  );
}

export function ConfigurationView() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-64 w-full" />
        </div>
      }
    >
      <ConfigurationBody />
    </Suspense>
  );
}
