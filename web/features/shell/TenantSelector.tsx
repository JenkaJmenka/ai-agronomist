"use client";

import { parseAsString, useQueryStates } from "nuqs";

/**
 * Tenant/greenhouse selection lives in the URL (plan §7.4) so every page
 * link, reload, and share preserves context. Options are static in P0;
 * P1 wires them to /api/bff/readings/greenhouses.
 */
export function TenantSelector() {
  const [{ tenant, gh }, setScope] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
  });

  return (
    <div className="flex flex-col gap-2 border-t border-border px-4 py-3">
      <label className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        Tenant
        <select
          data-testid="tenant-select"
          className="mt-1 block w-full rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm text-text"
          value={tenant}
          onChange={(e) => void setScope({ tenant: e.target.value })}
        >
          <option value="tenant_1">tenant_1</option>
          <option value="tenant_2">tenant_2</option>
          <option value="tenant_3">tenant_3</option>
        </select>
      </label>
      <label className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        Greenhouse
        <select
          data-testid="greenhouse-select"
          className="mt-1 block w-full rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm text-text"
          value={gh}
          onChange={(e) => void setScope({ gh: e.target.value })}
        >
          <option value="gh_A">gh_A</option>
          <option value="gh_B">gh_B</option>
          <option value="gh_C">gh_C</option>
        </select>
      </label>
    </div>
  );
}
