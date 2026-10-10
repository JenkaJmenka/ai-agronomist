"use client";

import { parseAsString, useQueryState } from "nuqs";

/**
 * Greenhouse selection lives in the URL (plan §7.4) so every page link,
 * reload, and share preserves context. Options are static in P0; P1 wires
 * them to /api/bff/readings/greenhouses.
 *
 * Tenant selection used to live alongside this as `TenantSelector`, but the
 * BFF now derives the tenant from the signed-in session and ignores
 * whatever the client sends (security phase 2) — a tenant dropdown would
 * silently do nothing, so it was removed rather than kept as a no-op
 * control. Greenhouse is unrelated — it isn't identity-bound — so it stays
 * client-driven here.
 */
export function GreenhouseSelector() {
  const [gh, setGh] = useQueryState("gh", parseAsString.withDefault("gh_A"));

  return (
    <div className="flex flex-col gap-2 border-t border-border px-4 py-3">
      <label className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        Greenhouse
        <select
          data-testid="greenhouse-select"
          className="mt-1 block w-full rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm text-text"
          value={gh}
          onChange={(e) => void setGh(e.target.value)}
        >
          <option value="gh_A">gh_A</option>
          <option value="gh_B">gh_B</option>
          <option value="gh_C">gh_C</option>
        </select>
      </label>
    </div>
  );
}
