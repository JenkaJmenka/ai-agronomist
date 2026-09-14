"use client";

import { useQuery } from "@tanstack/react-query";

import { fetchReadingsBounds } from "@/features/monitoring/api/readings";
import { STALE_TIME_MS } from "@/lib/query";

/**
 * Oldest/newest stored reading timestamps for a scope.
 *
 * Cached per tenant/greenhouse, so the many MonitoringJumpLink instances on
 * the Anomalies page share a single request.
 */
export function useReadingsBounds(params: { tenant: string; gh: string }): {
  minTimestamp: string | null;
  maxTimestamp: string | null;
} {
  const { data } = useQuery({
    queryKey: ["readings-bounds", params.tenant, params.gh],
    queryFn: ({ signal }) => fetchReadingsBounds({ ...params, signal }),
    staleTime: STALE_TIME_MS.bounds,
  });

  return {
    minTimestamp: data?.min_timestamp ?? null,
    maxTimestamp: data?.max_timestamp ?? null,
  };
}
