"use client";

import { useQuery } from "@tanstack/react-query";

import {
  type Freshness,
  fetchLiveStatus,
  type GreenhouseStatus,
  type SourceStatus,
} from "@/features/monitoring/api/live";
import { useScope } from "@/lib/useScope";

/** Re-check freshness often enough to catch a feed going silent while the
 * page stays open — a scope can sit "live" with zero rows for days (see
 * `src/api/services/live_freshness.py` module docstring) and nothing short
 * of a periodic refetch surfaces that without a manual reload. */
const REFETCH_INTERVAL_MS = 30_000;

export interface UseLiveStatusResult {
  readonly greenhouseStatus: GreenhouseStatus | null;
  readonly sources: SourceStatus[];
  /** `null` while loading, erroring, or against an older API (deploy skew). */
  readonly freshness: Freshness | null;
  readonly isLoading: boolean;
  readonly isError: boolean;
}

/**
 * `GET /api/bff/live/status` for the current tenant/scope, independent of
 * the local `live` URL toggle — the idle/paused state must be visible even
 * when the operator hasn't turned Go Live on, and a stale *live* scope must
 * be caught whether or not anyone is actively watching the SSE stream.
 *
 * Shares its query key with `LiveBadge`'s sources fetch so both consumers
 * read the same cached response instead of issuing duplicate requests.
 */
export function useLiveStatus(): UseLiveStatusResult {
  const { tenant, gh } = useScope();

  const query = useQuery({
    queryKey: ["live-status", tenant, gh],
    queryFn: () => fetchLiveStatus({ tenant, gh }),
    staleTime: 15_000,
    refetchInterval: REFETCH_INTERVAL_MS,
  });

  return {
    greenhouseStatus: query.data?.greenhouse_status ?? null,
    sources: query.data?.sources ?? [],
    freshness: query.data?.freshness ?? null,
    isLoading: query.isLoading,
    isError: query.isError,
  };
}
