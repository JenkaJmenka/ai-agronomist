"use client";

import { useQuery } from "@tanstack/react-query";

import { fetchLearningProgress, type LearningProgress } from "@/features/feedback/api/feedback";
import { STALE_TIME_MS } from "@/lib/query";

export interface LearningProgressState {
  readonly progress: LearningProgress | null;
  readonly isLoading: boolean;
  readonly isError: boolean;
}

/** Progress toward the next retrain — invalidated after each verdict. */
export function useLearningProgress(tenant: string): LearningProgressState {
  const query = useQuery<LearningProgress, Error>({
    queryKey: ["learning-progress", tenant],
    queryFn: () => fetchLearningProgress(tenant),
    staleTime: STALE_TIME_MS.incidents,
    retry: 1,
  });
  return {
    progress: query.data ?? null,
    isLoading: query.isPending,
    isError: query.isError,
  };
}
