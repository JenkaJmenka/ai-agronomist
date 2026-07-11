"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { cancelJob, fetchJobs, type JobSummary } from "@/features/jobs/api/jobs";
import { STALE_TIME_MS } from "@/lib/query";

/** List refresh cadence — jobs are never trusted from cache (plan §4.3). */
const LIST_POLL_MS = 5000;

export interface JobsState {
  readonly jobs: JobSummary[];
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly error: Error | null;
  readonly terminate: (jobId: string) => void;
  readonly isTerminating: boolean;
  readonly terminateError: Error | null;
}

/** Live job list for the tenant/greenhouse scope + terminate mutation. */
export function useJobs(tenant: string, gh: string): JobsState {
  const queryClient = useQueryClient();

  const query = useQuery<JobSummary[], Error>({
    queryKey: ["jobs", tenant, gh],
    queryFn: () => fetchJobs({ tenant, gh }),
    staleTime: STALE_TIME_MS.jobStatus,
    refetchInterval: LIST_POLL_MS,
  });

  const terminateMutation = useMutation<void, Error, string>({
    mutationFn: cancelJob,
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  return {
    jobs: query.data ?? [],
    isLoading: query.isPending,
    isError: query.isError,
    error: query.error,
    terminate: (jobId) => terminateMutation.mutate(jobId),
    isTerminating: terminateMutation.isPending,
    terminateError: terminateMutation.error,
  };
}
