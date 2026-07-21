"use client";

import { parseAsInteger, useQueryState } from "nuqs";

import { DEFAULT_REFRESH_SECONDS } from "@/lib/periodPresets";

/**
 * URL-backed live refresh interval (seconds). Default 30.
 * Shared by DateRangeControls and useReadings so both stay in sync.
 */
export function useRefreshInterval(): [number, (seconds: number) => Promise<URLSearchParams>] {
  const [refresh, setRefresh] = useQueryState(
    "refresh",
    parseAsInteger.withDefault(DEFAULT_REFRESH_SECONDS),
  );
  return [refresh, setRefresh];
}
