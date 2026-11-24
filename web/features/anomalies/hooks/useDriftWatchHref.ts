"use client";

import { useSearchParams } from "next/navigation";

import { withDashboardQuery } from "@/lib/dashboardQuery";
import { useScope } from "@/lib/useScope";

/**
 * Link to the Drift Watch page for the current scope. The page has no
 * per-episode deep link yet, so only tenant/gh and the shared dashboard
 * params are carried.
 */
export function useDriftWatchHref(): string {
  const search = useSearchParams();
  const { tenant, gh } = useScope();
  const params = new URLSearchParams(search?.toString() ?? "");
  params.set("tenant", tenant);
  params.set("gh", gh);
  return withDashboardQuery("/drift-watch", params);
}
