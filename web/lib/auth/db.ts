/**
 * Auth.js persistence — a dedicated Postgres instance (NOT the FastAPI
 * database owned by `src/`) holding users/accounts/sessions/verification
 * tokens (`@auth/pg-adapter` schema, see `lib/auth/schema.sql`) plus the
 * `user_tenants` mapping this app adds on top (security phase 2, §1).
 *
 * `AUTH_DATABASE_URL` is a new env var — provisioning it (Railway Postgres
 * plugin) is a deploy decision left to the coordinator, not done here. The
 * pool below must construct even when it's unset (`next build` imports this
 * module while collecting route metadata); an unset URL only surfaces as a
 * connection error the first time a query actually runs.
 */

import { Pool } from "pg";

let pool: Pool | undefined;

function getPool(): Pool {
  if (!pool) {
    pool = new Pool({ connectionString: process.env.AUTH_DATABASE_URL });
  }
  return pool;
}

/** Used by `auth.ts` to hand the pool to `@auth/pg-adapter`. */
export function getAuthPool(): Pool {
  return getPool();
}

/**
 * Resolves the single tenant a signed-in email belongs to. Returns `null`
 * when the email has no provisioned mapping — callers must treat that as
 * "deny", never as "fall back to a default tenant".
 */
export async function getTenantIdForEmail(email: string): Promise<string | null> {
  const result = await getPool().query<{ tenant_id: string }>(
    "select tenant_id from user_tenants where email = $1",
    [email.trim().toLowerCase()],
  );
  return result.rows.at(0)?.tenant_id ?? null;
}
