/**
 * Auth.js persistence — the existing `ai-agronomist-db` Postgres, in a
 * dedicated `auth` schema (NOT `public`, which the FastAPI app's alembic
 * metadata owns; sharing it would make `alembic revision --autogenerate`
 * see these tables as unknown and emit `DROP TABLE` for them). Holds
 * `@auth/pg-adapter`'s tables (see `lib/auth/schema.sql`) plus the
 * `user_tenants` mapping this app adds on top (security phase 2, §1).
 *
 * `@auth/pg-adapter` queries unqualified table names (`select * from users`,
 * etc.) — it has no schema option — so the pool below sets `search_path` to
 * `auth` for every connection instead of forking the adapter. That replaces
 * Postgres's default search_path entirely (no `public` fallback), which is
 * deliberate: this pool only ever touches Auth.js tables, so it must not be
 * able to silently resolve a same-named table in `public`.
 *
 * `AUTH_DATABASE_URL` is a new env var — provisioning it (Railway) is a
 * deploy decision left to the coordinator, not done here. The pool below
 * must construct even when it's unset (`next build` imports this module
 * while collecting route metadata); an unset URL only surfaces as a
 * connection error the first time a query actually runs.
 */

import { Pool } from "pg";

let pool: Pool | undefined;

function getPool(): Pool {
  if (!pool) {
    pool = new Pool({
      connectionString: process.env.AUTH_DATABASE_URL,
      options: "-c search_path=auth",
    });
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
 *
 * Table is schema-qualified explicitly rather than relying solely on the
 * pool's `search_path`: belt and braces against a `search_path` regression
 * silently reading the wrong table.
 */
export async function getTenantIdForEmail(email: string): Promise<string | null> {
  const result = await getPool().query<{ tenant_id: string }>(
    "select tenant_id from auth.user_tenants where email = $1",
    [email.trim().toLowerCase()],
  );
  return result.rows.at(0)?.tenant_id ?? null;
}
