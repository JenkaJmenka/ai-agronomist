"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  activateModel,
  deleteModelVersion,
  fetchModelVersions,
  type ModelVersion,
} from "@/features/operations/api/operations";
import { STALE_TIME_MS } from "@/lib/query";

export interface ModelVersionsState {
  readonly versions: ModelVersion[];
  readonly activeVersion: ModelVersion | null;
  readonly isLoading: boolean;
  readonly isError: boolean;
  readonly error: Error | null;
  readonly activate: (modelId: string) => void;
  readonly remove: (modelId: string) => void;
  readonly isMutating: boolean;
  readonly mutationError: Error | null;
}

/** Model registry list + activate/delete mutations, scoped by tenant. */
export function useModelVersions(tenant: string): ModelVersionsState {
  const queryClient = useQueryClient();
  const queryKey = ["model-versions", tenant];

  const query = useQuery<ModelVersion[], Error>({
    queryKey,
    queryFn: () => fetchModelVersions(tenant),
    staleTime: STALE_TIME_MS.activeModelVersion,
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey });

  const activateMutation = useMutation<{ status: string }, Error, string>({
    mutationFn: (modelId) => activateModel(tenant, modelId),
    onSettled: invalidate,
  });

  const deleteMutation = useMutation<{ status: string }, Error, string>({
    mutationFn: (modelId) => {
      const versions = query.data ?? [];
      const target = versions.find((version) => version.model_id === modelId);
      // Deleting the only active model needs force=true (mirrors Streamlit).
      const force = Boolean(target?.is_active) && versions.length === 1;
      return deleteModelVersion({ tenant, modelId, force });
    },
    onSettled: invalidate,
  });

  const versions = query.data ?? [];
  return {
    versions,
    activeVersion: versions.find((version) => version.is_active) ?? null,
    isLoading: query.isPending,
    isError: query.isError,
    error: query.error,
    activate: (modelId) => activateMutation.mutate(modelId),
    remove: (modelId) => deleteMutation.mutate(modelId),
    isMutating: activateMutation.isPending || deleteMutation.isPending,
    mutationError: activateMutation.error ?? deleteMutation.error,
  };
}
