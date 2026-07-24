"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchCropProfiles,
  fetchOperationalConfig,
  type OperationalConfigDocument,
  putOperationalConfig,
} from "@/features/configuration/api/operationalConfig";
import { STALE_TIME_MS } from "@/lib/query";

export function useOperationalConfig(tenant: string, gh: string) {
  const queryClient = useQueryClient();
  const queryKey = ["operational-config", tenant, gh] as const;

  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => fetchOperationalConfig({ tenant, gh, signal }),
    staleTime: STALE_TIME_MS.windowedReadings,
  });

  const profiles = useQuery({
    queryKey: ["crop-profiles"],
    queryFn: ({ signal }) => fetchCropProfiles(signal),
    staleTime: 60_000,
  });

  const save = useMutation({
    mutationFn: (config: OperationalConfigDocument) => putOperationalConfig({ tenant, gh, config }),
    onSuccess: (data) => {
      queryClient.setQueryData(queryKey, data);
    },
  });

  return {
    config: query.data?.config ?? null,
    sources: query.data?.sources ?? {},
    isLoading: query.isPending,
    isError: query.isError,
    error: query.error,
    cropProfiles: profiles.data ?? null,
    save,
  };
}
