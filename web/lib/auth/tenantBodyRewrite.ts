/**
 * Overwrites a `tenant_id` field in a JSON request body with the session's
 * tenant (security phase 2 follow-up — the BFF must be the single source of
 * truth for tenant, the same way it already is for the `X-Tenant-Id` header
 * and the `tenant_id` query param). `POST /api/incidents/store`,
 * `/resolve/{id}`, and `/api/backfill/scores` all take tenant from the JSON
 * body, so a stale FE-supplied value there 403s exactly like the query
 * param does.
 *
 * Never adds the field, never rejects a mismatch — only substitutes the
 * correct value when the field is already present, mirroring the
 * overwrite-don't-reject rule already applied to the query param in the
 * route itself. Malformed JSON, a non-object body, or a body with no
 * `tenant_id` field passes through byte-for-byte unchanged — that is
 * upstream's problem to reject, not this rewrite's.
 */
export function overwriteTenantIdInJsonBody(rawBody: string, tenantId: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawBody);
  } catch {
    return rawBody;
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return rawBody;
  }
  if (!("tenant_id" in parsed)) {
    return rawBody;
  }

  return JSON.stringify({ ...parsed, tenant_id: tenantId });
}
