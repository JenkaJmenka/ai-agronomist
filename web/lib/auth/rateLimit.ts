/**
 * Durable sign-in rate limiting (security phase 2, magic-link hardening,
 * item b): 5 requests per email per hour, 20 per IP per hour. Backed by
 * Postgres (`auth.signin_attempts`, see `lib/auth/schema.sql`), not
 * in-process memory — Railway can restart `web` or run more than one
 * instance, and an in-memory counter would silently reset to zero on either.
 * Deliberately not Redis: this project's Redis belongs to the API
 * (`fastapi-cache`), and coupling `web` to it would cross a service
 * boundary for no benefit when a Postgres pool already exists here.
 *
 * The orchestration (`isWithinRateLimit`) takes its storage as a
 * `SignInAttemptStore` so it's unit-testable with a fake, in-memory store —
 * no test in this repo talks to a real database.
 */

import { getAuthPool } from "./db";

export const EMAIL_ATTEMPT_LIMIT_PER_HOUR = 5;
export const IP_ATTEMPT_LIMIT_PER_HOUR = 20;
const WINDOW_MS = 60 * 60 * 1000;

export type SignInAttemptScope = "email" | "ip";

export interface SignInAttemptStore {
  /** Deletes attempts for (scope, key) older than the window. */
  pruneExpired(scope: SignInAttemptScope, key: string, cutoff: number): Promise<void>;
  /** Counts remaining (i.e. non-expired) attempts for (scope, key). */
  count(scope: SignInAttemptScope, key: string): Promise<number>;
  /** Records one attempt for (scope, key) at `timestamp`. */
  record(scope: SignInAttemptScope, key: string, timestamp: number): Promise<void>;
}

/**
 * Prunes expired attempts, then allows the new one and records it only if
 * still under `limit`. Returns `false` (deny, do not record) once at or
 * over the limit — callers must not act on a denied attempt.
 */
export async function isWithinRateLimit(
  store: SignInAttemptStore,
  scope: SignInAttemptScope,
  key: string,
  limit: number,
  now: () => number = Date.now,
): Promise<boolean> {
  const timestamp = now();
  await store.pruneExpired(scope, key, timestamp - WINDOW_MS);
  const count = await store.count(scope, key);
  if (count >= limit) return false;
  await store.record(scope, key, timestamp);
  return true;
}

/** Real, Postgres-backed store — `auth.signin_attempts` via the shared pool. */
export function createPostgresSignInAttemptStore(): SignInAttemptStore {
  return {
    async pruneExpired(scope, key, cutoff) {
      await getAuthPool().query(
        "delete from auth.signin_attempts where scope = $1 and key = $2 and created_at <= $3",
        [scope, key, new Date(cutoff)],
      );
    },
    async count(scope, key) {
      const result = await getAuthPool().query<{ n: string }>(
        "select count(*)::text as n from auth.signin_attempts where scope = $1 and key = $2",
        [scope, key],
      );
      return Number(result.rows.at(0)?.n ?? 0);
    },
    async record(scope, key, timestamp) {
      await getAuthPool().query(
        "insert into auth.signin_attempts (scope, key, created_at) values ($1, $2, $3)",
        [scope, key, new Date(timestamp)],
      );
    },
  };
}
