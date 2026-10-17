"use client";

import { useQuery } from "@tanstack/react-query";

import {
  type DetectorState,
  fetchLiveStatus,
  type SourceStatus,
} from "@/features/monitoring/api/live";
import type { LiveTransport } from "@/features/monitoring/hooks/useLiveReadings";
import { useScope } from "@/lib/useScope";

interface LiveBadgeProps {
  readonly transport: LiveTransport;
  readonly stale: boolean;
  /** Per-source statuses from SSE events (merged with initial fetch). */
  readonly sourceStatuses: SourceStatus[];
}

// F12: use sev-* design tokens (MIGRATION_PLAN §7.2) instead of raw palette classes
// so status colours are consistent with SensorTile and respect dark-mode overrides.
const STATUS_DOT: Record<SourceStatus["status"], string> = {
  online: "bg-sev-normal",
  degraded: "bg-sev-warning",
  offline: "bg-sev-critical",
  // A paused scope is off, not broken — muted rather than a severity colour.
  paused: "bg-muted-foreground/40",
};

/**
 * Fetches the initial source list and the scope's detector state; SSE
 * `source-status` events keep the sources fresh afterwards.
 *
 * `detector_state` has no SSE counterpart, so this fetch is the only source
 * for it — hence the modest staleTime rather than a one-shot read.
 */
function useInitialStatus(enabled: boolean): {
  sources: SourceStatus[];
  detectorState: DetectorState;
} {
  const { tenant, gh } = useScope();

  const query = useQuery({
    queryKey: ["live-status", tenant, gh],
    queryFn: () => fetchLiveStatus({ tenant, gh }),
    enabled,
    staleTime: 30_000,
  });

  return {
    sources: query.data?.sources ?? [],
    // Absent on an older API mid rolling-deploy: treat as "no information"
    // rather than asserting a problem.
    detectorState: query.data?.detector_state ?? "unknown",
  };
}

/**
 * Status pill for live mode.
 *
 * - Green pulse "Live · SSE"
 * - Amber       "Live · polling"
 * - Red         "Stalled"
 * - Gray        "Paused"
 * - Amber       "Live · not analysed"  (streaming, but no detector resolved)
 *
 * The not-analysed case outranks the transport label on purpose: a scope with
 * no deployed model persists readings and scores none of them, and showing a
 * healthy green "Live · SSE" for it is precisely the misread this fixes.
 *
 * Source dots below the label reflect per-source health received via
 * `source-status` events merged with the initial `/api/bff/live/status` fetch.
 */
export function LiveBadge({ transport, stale, sourceStatuses }: LiveBadgeProps) {
  const isActive = transport !== "idle";
  const { sources: initialSources, detectorState } = useInitialStatus(isActive);

  // Merge initial + SSE-updated sources; SSE wins on source_id conflict.
  const mergedSources = (() => {
    const map = new Map<string, SourceStatus>();
    for (const s of initialSources) map.set(s.source_id, s);
    for (const s of sourceStatuses) map.set(s.source_id, s);
    return Array.from(map.values());
  })();

  if (transport === "idle") {
    return null;
  }

  const { label, pillClass, dotClass } = getBadgeStyle({
    transport,
    stale,
    detectorState,
  });

  return (
    <div
      className="flex flex-col items-end gap-1"
      data-testid="live-badge"
      data-transport={transport}
      data-stale={stale ? "true" : "false"}
      data-detector-state={detectorState}
    >
      <span
        className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold ${pillClass}`}
      >
        <span className={`h-2 w-2 rounded-full ${dotClass}`} aria-hidden="true" />
        {label}
      </span>

      {mergedSources.length > 0 && (
        <div className="flex items-center gap-1" data-testid="live-badge-sources">
          {mergedSources.map((s) => (
            <span
              key={s.source_id}
              role="img"
              title={`${s.source_id} (${s.vendor}): ${s.status}`}
              className={`h-2 w-2 rounded-full ${STATUS_DOT[s.status]}`}
              aria-label={`${s.source_id}: ${s.status}`}
              data-testid={`source-dot-${s.source_id}`}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface BadgeStyle {
  readonly label: string;
  readonly pillClass: string;
  readonly dotClass: string;
}

function getBadgeStyle({
  transport,
  stale,
  detectorState,
}: {
  transport: LiveTransport;
  stale: boolean;
  detectorState: DetectorState;
}): BadgeStyle {
  // Stalled still wins: no data arriving is a worse problem than data arriving
  // unscored, and the two are not mutually exclusive.
  if (stale) {
    return {
      label: "Stalled",
      pillClass: "bg-sev-critical/15 text-sev-critical",
      dotClass: "bg-sev-critical",
    };
  }
  if (detectorState === "missing" || detectorState === "error") {
    return {
      label: "Live · not analysed",
      pillClass: "bg-sev-warning/15 text-sev-warning",
      dotClass: "bg-sev-warning",
    };
  }
  if (transport === "sse") {
    return {
      label: "Live · SSE",
      pillClass: "bg-sev-normal/15 text-sev-normal",
      // F12: motion-safe: respects prefers-reduced-motion (mirrors SensorTile's
      // tile-flash guard in globals.css) — pulse is decorative, not informational.
      dotClass: "motion-safe:animate-pulse bg-sev-normal",
    };
  }
  // polling
  return {
    label: "Live · polling",
    pillClass: "bg-sev-warning/15 text-sev-warning",
    dotClass: "bg-sev-warning",
  };
}
