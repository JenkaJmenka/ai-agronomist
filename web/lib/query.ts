/** TanStack Query defaults — staleTimes per MIGRATION_PLAN.md §4.3. */

import { QueryClient } from "@tanstack/react-query";

export const STALE_TIME_MS = {
  bounds: 5 * 60_000,
  greenhouses: 5 * 60_000,
  windowedReadings: 2 * 60_000,
  anomaliesInWindow: 60_000,
  activeModelVersion: 60_000,
  pipelineMetrics: 30_000,
  /** Job status is never trusted from cache — SSE or short polling only. */
  jobStatus: 0,
  incidents: 60_000,
} as const;

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: STALE_TIME_MS.windowedReadings,
        refetchOnWindowFocus: false,
        retry: 1,
      },
    },
  });
}
