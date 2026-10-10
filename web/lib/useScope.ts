"use client";

import { parseAsString, useQueryStates } from "nuqs";

import { useSessionTenantId } from "@/lib/SessionTenantContext";

export interface Scope {
  readonly tenant: string;
  readonly gh: string;
}

/**
 * Tenant/greenhouse scope from the URL (plan §7.4) — shared by data hooks.
 * `tenant` defaults to the signed-in session's tenant (see
 * `SessionTenantContext`), falling back to `"tenant_1"` only when there is
 * no session yet (e.g. a render before hydration); `?tenant=` in the URL
 * still overrides either default.
 */
export function useScope(): Scope {
  const sessionTenantId = useSessionTenantId();
  const [{ tenant, gh }] = useQueryStates({
    tenant: parseAsString.withDefault(sessionTenantId ?? "tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
  });
  return { tenant, gh };
}
