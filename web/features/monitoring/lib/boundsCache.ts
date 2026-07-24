/** Process-local cache of the newest reading timestamp per tenant/gh scope. */

const boundsMaxByScope = new Map<string, string>();

function scopeKey(tenant: string, gh: string): string {
  return `${tenant}/${gh}`;
}

/** Remember the newest stored reading so period snaps need not wait on bounds. */
export function rememberBoundsMax(tenant: string, gh: string, maxTimestamp: string | null): void {
  if (!maxTimestamp) return;
  boundsMaxByScope.set(scopeKey(tenant, gh), maxTimestamp);
}

/** Last known max timestamp for the scope, if any. */
export function getCachedBoundsMax(tenant: string, gh: string): Date | null {
  const raw = boundsMaxByScope.get(scopeKey(tenant, gh));
  if (!raw) return null;
  const ms = Date.parse(raw);
  return Number.isFinite(ms) ? new Date(ms) : null;
}
