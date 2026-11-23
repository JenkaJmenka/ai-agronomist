"use client";

import { Suspense } from "react";

import { Card } from "@/components/ui/Card";
import { Chip } from "@/components/ui/Chip";
import { Skeleton } from "@/components/ui/Skeleton";
import type { DriftWatchEpisode, DriftWatchInput } from "@/features/drift-watch/api/driftWatch";
import {
  useDriftWatchEpisodes,
  useDriftWatchInputs,
} from "@/features/drift-watch/hooks/useDriftWatch";
import type { Severity } from "@/lib/severity";
import { useScope } from "@/lib/useScope";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

/** Format an ISO datetime to "DD/MM/YY HH:MM" in local time. */
function formatTs(iso: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  const dt = new Date(ms);
  const date = dt.toLocaleDateString(undefined, {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  });
  const time = dt.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${date} ${time}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function InputRow({ input }: { readonly input: DriftWatchInput }) {
  const far =
    input.false_alarm_rate != null ? `${input.false_alarm_rate.toFixed(2)}/mo false alarms` : null;
  const dr =
    input.detection_rate != null ? `${(input.detection_rate * 100).toFixed(0)}% detection` : null;

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
        </div>
        {input.status_reason ? (
          <p className="mt-0.5 text-sm text-text-muted" data-testid="input-status-reason">
            {input.status_reason}
          </p>
        ) : null}
      </div>
      {(far ?? dr) ? (
        <div className="shrink-0 text-right text-sm text-text-muted tabular-nums">
          {far ? <div>{far}</div> : null}
          {dr ? <div>{dr}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

function EpisodeRow({ episode }: { readonly episode: DriftWatchEpisode }) {
  const isShadow = !episode.armed;
  const rowClass = isShadow ? "opacity-60" : "";

  // offset_estimate is a magnitude; the sign comes from episode.direction.
  const offsetStr =
    episode.offset_estimate != null
      ? `${episode.direction === "-" ? "-" : "+"}${Math.abs(episode.offset_estimate).toFixed(3)}`
      : null;

  return (
    <tr
      data-testid="drift-episode-row"
      data-armed={String(episode.armed)}
      className={`border-b border-border last:border-0 ${rowClass}`}
    >
      <td className="py-2 pr-4 text-sm tabular-nums text-text-muted">
        {formatTs(episode.first_detected_ts)}
      </td>
      <td className="py-2 pr-4 text-sm font-medium text-text">{episode.target_channel}</td>
      <td className="py-2 pr-4 text-sm tabular-nums text-text">{episode.direction}</td>
      <td className="py-2 pr-4 text-sm tabular-nums text-text">
        {offsetStr ?? <span className="text-text-muted">—</span>}
      </td>
      <td className="py-2 pr-4 text-sm">
        {episode.ongoing ? (
          <span className="text-sev-warning font-medium">Yes</span>
        ) : (
          <span className="text-text-muted">No</span>
        )}
      </td>
      <td className="py-2">
        {episode.armed ? (
          <span data-testid="episode-armed-chip">
            <Chip severity="normal">Armed</Chip>
          </span>
        ) : (
          <span data-testid="episode-shadow-chip">
            <Chip severity="unclassified">Shadow (informational)</Chip>
          </span>
        )}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Main body
// ---------------------------------------------------------------------------

function DriftWatchBody() {
  const { tenant, gh } = useScope();
  const {
    inputs,
    isLoading: inputsLoading,
    isError: inputsError,
    error: inputsErr,
  } = useDriftWatchInputs(tenant, gh);
  const {
    episodes,
    isLoading: episodesLoading,
    isError: episodesError,
    error: episodesErr,
  } = useDriftWatchEpisodes(tenant, gh);

  const isLoading = inputsLoading || episodesLoading;

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-bold">Drift Watch</h1>
          <Chip severity="warning">Beta</Chip>
        </div>
        <p className="mt-1 text-sm text-text-muted">
          Drift watch detects slow sensor drift by comparing sensors that should agree. It needs
          about 16 weeks of history to calibrate. Inputs marked{" "}
          <span className="font-medium">shadow</span> are tracked but not yet trusted to alert.
        </p>
      </div>

      {/* Inputs section */}
      <Card title="Inputs">
        {inputsError ? (
          <p role="alert" className="py-4 text-center text-sm text-sev-critical">
            Could not load inputs: {inputsErr?.message}
          </p>
        ) : isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : inputs.length === 0 ? (
          <p className="py-6 text-center text-sm text-text-muted">No drift watch inputs found.</p>
        ) : (
          <div data-testid="inputs-list">
            {inputs.map((input) => (
              <InputRow key={input.input_id} input={input} />
            ))}
          </div>
        )}
      </Card>

      {/* Episodes section */}
      <Card title="Episodes">
        {episodesError ? (
          <p role="alert" className="py-4 text-center text-sm text-sev-critical">
            Could not load episodes: {episodesErr?.message}
          </p>
        ) : isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : episodes.length === 0 ? (
          <p
            className="py-8 text-center text-sm text-text-muted"
            data-testid="episodes-empty-state"
          >
            No drift episodes recorded yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left" data-testid="episodes-table">
              <thead>
                <tr className="border-b border-border">
                  <th className="pb-2 pr-4 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    First detected
                  </th>
                  <th className="pb-2 pr-4 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Channel
                  </th>
                  <th className="pb-2 pr-4 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Direction
                  </th>
                  <th className="pb-2 pr-4 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Est. offset
                  </th>
                  <th className="pb-2 pr-4 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Ongoing
                  </th>
                  <th className="pb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Status
                  </th>
                </tr>
              </thead>
              <tbody>
                {episodes.map((ep) => (
                  <EpisodeRow key={ep.episode_id} episode={ep} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Exported view
// ---------------------------------------------------------------------------

export function DriftWatchView() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-48 w-full" />
          <Skeleton className="h-48 w-full" />
        </div>
      }
    >
      <DriftWatchBody />
    </Suspense>
  );
}
