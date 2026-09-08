"use client";

import { Suspense, useMemo, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { TwoStepConfirm } from "@/components/ui/TwoStepConfirm";
import { useHomeLayoutPrefs } from "@/features/app-configuration/hooks/useHomeLayoutPrefs";
import {
  DEFAULT_HOME_LAYOUT_PREFS,
  type HomeLayoutPrefs,
} from "@/features/app-configuration/lib/homeLayoutPrefs";
import { HOME_CLIMATE_PRIORITY } from "@/features/home/lib/homeChannels";
import { PERIOD_PRESETS } from "@/lib/periodPresets";

const CHANNEL_OPTIONS = [
  ...HOME_CLIMATE_PRIORITY,
  "c2h4",
  "gas_co2",
  "no",
  "no2",
  "nox",
  "co",
] as const;

function AppConfigurationBody() {
  const { prefs, setPrefs, resetPrefs, hydrated } = useHomeLayoutPrefs();
  const [draft, setDraft] = useState<HomeLayoutPrefs | null>(null);
  const effective = draft ?? prefs;

  const isDirty = useMemo(() => {
    if (!hydrated) return false;
    return JSON.stringify(effective) !== JSON.stringify(prefs);
  }, [effective, prefs, hydrated]);

  if (!hydrated) {
    return (
      <div className="mx-auto max-w-6xl space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  function update(partial: Partial<HomeLayoutPrefs>): void {
    setDraft({ ...effective, ...partial });
  }

  function toggleChannel(channel: string): void {
    const set = new Set(effective.primaryChannels);
    if (set.has(channel)) set.delete(channel);
    else set.add(channel);
    update({ primaryChannels: CHANNEL_OPTIONS.filter((ch) => set.has(ch)) });
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6" data-testid="app-configuration-page">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">App Configuration</h1>
          <p className="text-sm text-text-muted">
            Home layout preferences for this browser (localStorage).
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="ghost"
            disabled={!isDirty}
            onClick={() => setDraft(null)}
            data-testid="app-config-discard"
          >
            Discard
          </Button>
          <TwoStepConfirm
            testId="app-config-reset"
            triggerLabel="Reset defaults"
            confirmLabel="Confirm reset"
            warning="Reset Home layout prefs to the Phase A defaults for this browser."
            onConfirm={() => {
              resetPrefs();
              setDraft(null);
            }}
          />
          <TwoStepConfirm
            testId="app-config-save"
            triggerLabel={isDirty ? "Save changes" : "Saved"}
            confirmLabel="Confirm save"
            warning="Saving updates Home widget visibility, primary channels, and default period on this device."
            disabled={!isDirty}
            triggerVariant="primary"
            onConfirm={() => {
              setPrefs(effective);
              setDraft(null);
            }}
          />
        </div>
      </div>

      <Card title="Home widgets">
        <div className="flex flex-col gap-2 text-sm">
          {(
            [
              ["showStatusBar", "Status bar (live + anomaly count)"],
              ["showTiles", "Sensor pulse tiles"],
              ["showChart", "Climate overview chart"],
              ["showAnomalyList", "Recent anomalies list"],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={effective[key]}
                data-testid={`app-config-${key}`}
                onChange={(e) => update({ [key]: e.target.checked })}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
      </Card>

      <Card title="Primary channels (tiles & chart)">
        <p className="mb-3 text-sm text-text-muted">
          Choose 3–5 preferred channels. Home falls back to climate discovery if none are present in
          the data.
        </p>
        <div className="flex flex-wrap gap-2">
          {CHANNEL_OPTIONS.map((channel) => {
            const checked = effective.primaryChannels.includes(channel);
            return (
              <label
                key={channel}
                className={`inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-3 py-1 text-xs ${
                  checked
                    ? "border-accent bg-accent/10 text-accent"
                    : "border-border text-text-muted"
                }`}
              >
                <input
                  type="checkbox"
                  className="sr-only"
                  checked={checked}
                  data-testid={`app-config-channel-${channel}`}
                  onChange={() => toggleChannel(channel)}
                />
                {channel}
              </label>
            );
          })}
        </div>
      </Card>

      <Card title="Default Home period">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium text-text-muted">
            Applied when Home opens with no from/to
          </span>
          <select
            className="max-w-xs rounded-[var(--radius-control)] border border-border bg-surface px-3 py-2"
            value={effective.defaultPeriodId}
            data-testid="app-config-default-period"
            onChange={(e) => update({ defaultPeriodId: e.target.value })}
          >
            {PERIOD_PRESETS.map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.label}
              </option>
            ))}
          </select>
        </label>
        <p className="mt-2 text-xs text-text-muted">
          Defaults match Phase A ({DEFAULT_HOME_LAYOUT_PREFS.defaultPeriodId}) when unset.
        </p>
      </Card>
    </div>
  );
}

export function AppConfigurationView() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-64 w-full" />
        </div>
      }
    >
      <AppConfigurationBody />
    </Suspense>
  );
}
