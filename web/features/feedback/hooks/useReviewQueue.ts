"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchOpenIncidents,
  type OpenIncident,
  submitVerdict,
  type VerdictInput,
  type VerdictResponse,
} from "@/features/feedback/api/feedback";
import { STALE_TIME_MS } from "@/lib/query";

interface VerdictContext {
  readonly previous: OpenIncident[] | undefined;
}

export interface ReviewQueueState {
  readonly incidents: OpenIncident[];
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly error: Error | null;
  readonly submit: (input: Omit<VerdictInput, "tenant">) => void;
  readonly isSubmitting: boolean;
  readonly submitError: Error | null;
  readonly lastVerdict: VerdictResponse | null;
}

/**
 * Open-incident review queue with an optimistic verdict mutation
 * (plan §7.4): the labelled incident leaves the queue instantly, comes
 * back on error (rollback), and the queue + learning progress reconcile
 * with the server on settle. Fixes the Streamlit COR-1 "success message
 * discarded by rerun" bug by construction.
 */
export function useReviewQueue(tenant: string, gh: string): ReviewQueueState {
  const queryClient = useQueryClient();
  const queueKey = ["incidents-open", tenant, gh];

  const query = useQuery<OpenIncident[], Error>({
    queryKey: queueKey,
    queryFn: () => fetchOpenIncidents({ tenant, gh }),
    staleTime: STALE_TIME_MS.incidents,
  });

  const mutation = useMutation<
    VerdictResponse,
    Error,
    Omit<VerdictInput, "tenant">,
    VerdictContext
  >({
    mutationFn: (input) => submitVerdict({ ...input, tenant }),
    onMutate: async (input) => {
      await queryClient.cancelQueries({ queryKey: queueKey });
      const previous = queryClient.getQueryData<OpenIncident[]>(queueKey);
      queryClient.setQueryData<OpenIncident[]>(queueKey, (current) =>
        (current ?? []).filter((incident) => incident.id !== input.incidentId),
      );
      return { previous };
    },
    onError: (_error, _input, context) => {
      if (context?.previous) {
        queryClient.setQueryData(queueKey, context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queueKey });
      void queryClient.invalidateQueries({ queryKey: ["learning-progress", tenant] });
    },
  });

  return {
    incidents: query.data ?? [],
    isLoading: query.isPending,
    isError: query.isError,
    error: query.error,
    submit: (input) => mutation.mutate(input),
    isSubmitting: mutation.isPending,
    submitError: mutation.error,
    lastVerdict: mutation.data ?? null,
  };
}
