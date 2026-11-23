"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type DriftWatchEpisode,
  type DriftWatchFeedbackInput,
  type DriftWatchInput,
  fetchDriftWatchEpisodes,
  fetchDriftWatchInputs,
  postDriftWatchFeedback,
} from "@/features/drift-watch/api/driftWatch";
import { STALE_TIME_MS } from "@/lib/query";

/** Drift watch inputs (monitor specs) for the current tenant/greenhouse. */
export function useDriftWatchInputs(tenant: string, gh: string) {
  const query = useQuery<DriftWatchInput[], Error>({
    queryKey: ["drift-watch", "inputs", tenant, gh],
    queryFn: () => fetchDriftWatchInputs({ tenant, gh }),
    staleTime: STALE_TIME_MS.anomaliesInWindow,
  });

  return {
    inputs: query.data ?? [],
    isLoading: query.isPending,
    isError: query.isError,
    error: query.error,
  };
}

/** Drift watch episodes (newest first) for the current tenant/greenhouse. */
export function useDriftWatchEpisodes(tenant: string, gh: string) {
  const query = useQuery<DriftWatchEpisode[], Error>({
    queryKey: ["drift-watch", "episodes", tenant, gh],
    queryFn: () => fetchDriftWatchEpisodes({ tenant, gh }),
    staleTime: STALE_TIME_MS.anomaliesInWindow,
  });

  return {
    episodes: query.data ?? [],
    isLoading: query.isPending,
    isError: query.isError,
    error: query.error,
  };
}

/**
 * Label an episode. No optimistic state: the label shown is always what the
 * server returned on the refetch. The mutation stays pending until the
 * refetch settles, so the buttons cannot be pressed against stale data.
 */
export function useDriftWatchFeedback() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, DriftWatchFeedbackInput>({
    mutationFn: postDriftWatchFeedback,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["drift-watch"] }),
  });
}
