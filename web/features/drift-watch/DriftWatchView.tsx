"use client";

import { Suspense } from "react";

import { Card } from "@/components/ui/Card";
import { Chip } from "@/components/ui/Chip";
import { Skeleton } from "@/components/ui/Skeleton";
import { DriftEpisodeRow } from "@/features/drift-watch/components/DriftEpisodeRow";
import { DriftInputRow } from "@/features/drift-watch/components/DriftInputRow";
import {
  useDriftWatchEpisodes,
  useDriftWatchInputs,
} from "@/features/drift-watch/hooks/useDriftWatch";
import { useScope } from "@/lib/useScope";

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
              <DriftInputRow key={input.input_id} input={input} />
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
                  <th className="pb-2 pr-4 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Status
                  </th>
                  <th className="pb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Review
                  </th>
                </tr>
              </thead>
              <tbody>
                {episodes.map((ep) => (
                  <DriftEpisodeRow key={ep.episode_id} episode={ep} />
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
