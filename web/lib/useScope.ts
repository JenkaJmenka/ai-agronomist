"use client";

import { parseAsString, useQueryStates } from "nuqs";

export interface Scope {
  readonly tenant: string;
  readonly gh: string;
}

/** Tenant/greenhouse scope from the URL (plan §7.4) — shared by data hooks. */
export function useScope(): Scope {
  const [{ tenant, gh }] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
  });
  return { tenant, gh };
}
