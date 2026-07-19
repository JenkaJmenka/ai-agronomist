"use client";

import { useQuery } from "@tanstack/react-query";

import { type SourceStatus, statusResponseSchema } from "@/features/monitoring/api/live";
import type { LiveTransport } from "@/features/monitoring/hooks/useLiveReadings";
import { useScope } from "@/lib/useScope";

interface LiveBadgeProps {
  readonly transport: LiveTransport;
  readonly stale: boolean;
  /** Per-source statuses from SSE events (merged with initial fetch). */
  readonly sourceStatuses: SourceStatus[];
}

const STATUS_DOT: Record<SourceStatus["status"], string> = {
  online: "bg-green-400",
  degraded: "bg-amber-400",
  offline: "bg-red-400",
};

/** Fetches the initial source list; SSE `source-status` events keep it fresh. */
function useInitialSources(enabled: boolean): SourceStatus[] {
  const { tenant, gh } = useScope();

  const query = useQuery({
    queryKey: ["live-status", tenant, gh],
    queryFn: async () => {
      const params = new URLSearchParams({ tenant_id: tenant, greenhouse_id: gh });
      const res = await fetch(`/api/bff/live/status?${params}`);
      if (!res.ok) throw new Error(`Live status request failed (${res.status})`);
      return statusResponseSchema.parse(await res.json()).sources;
    },
    enabled,
    staleTime: 30_000,
  });

  return query.data ?? [];
}

/**
 * Status pill for live mode.
 *
 * - Green pulse "Live · SSE"
 * - Amber       "Live · polling"
 * - Red         "Stalled"
 * - Gray        "Paused"
 *
 * Source dots below the label reflect per-source health received via
 * `source-status` events merged with the initial `/api/bff/live/status` fetch.
 */
export function LiveBadge({ transport, stale, sourceStatuses }: LiveBadgeProps) {
  const isActive = transport !== "idle";
  const initialSources = useInitialSources(isActive);

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

  const { label, pillClass, dotClass } = getBadgeStyle({ transport, stale });

  return (
    <div
      className="flex flex-col items-end gap-1"
      data-testid="live-badge"
      data-transport={transport}
      data-stale={stale ? "true" : "false"}
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
}: {
  transport: LiveTransport;
  stale: boolean;
}): BadgeStyle {
  if (stale) {
    return {
      label: "Stalled",
      pillClass: "bg-red-500/15 text-red-500",
      dotClass: "bg-red-500",
    };
  }
  if (transport === "sse") {
    return {
      label: "Live · SSE",
      pillClass: "bg-green-500/15 text-green-600",
      dotClass: "animate-pulse bg-green-500",
    };
  }
  // polling
  return {
    label: "Live · polling",
    pillClass: "bg-amber-500/15 text-amber-600",
    dotClass: "bg-amber-500",
  };
}
