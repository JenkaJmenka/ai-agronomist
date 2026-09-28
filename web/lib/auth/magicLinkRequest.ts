/**
 * Orchestrates a magic-link sign-in request (security phase 2, magic-link
 * hardening, item c): the caller-visible outcome — return value, and how
 * long the call takes — must be identical whether the email is provisioned,
 * unprovisioned, or rate limited. The `signIn` callback in `auth.ts` already
 * denies an unprovisioned email before any mail is sent; what this function
 * adds is making sure nothing *else* leaks that decision back to the caller.
 *
 * Split out from the server action (`features/auth/actions.ts`) so it's
 * unit-testable with fake dependencies — no test here touches Postgres,
 * Resend, or Next.js's request/header APIs.
 */

import { EMAIL_ATTEMPT_LIMIT_PER_HOUR, IP_ATTEMPT_LIMIT_PER_HOUR } from "./rateLimit";

/** Floor on how long a request takes, so a fast-reject path can't be timed apart from a slow one. */
export const MIN_RESPONSE_MS = 400;

export interface MagicLinkRequestDeps {
  /** Durable rate-limit check; `false` means deny without sending mail. */
  isAllowed(scope: "email" | "ip", key: string): Promise<boolean>;
  /** Sends the magic link. Must itself swallow "unprovisioned email" errors. */
  sendMagicLink(email: string): Promise<void>;
  now(): number;
  sleep(ms: number): Promise<void>;
}

/**
 * Always resolves (never throws for a denied/unprovisioned/rate-limited
 * request) after at least `MIN_RESPONSE_MS` has elapsed, regardless of which
 * path was taken.
 */
export async function requestMagicLink(
  rawEmail: string,
  clientIp: string,
  deps: MagicLinkRequestDeps,
): Promise<void> {
  const start = deps.now();
  const email = rawEmail.trim().toLowerCase();

  if (email) {
    const [emailAllowed, ipAllowed] = await Promise.all([
      deps.isAllowed("email", email),
      deps.isAllowed("ip", clientIp),
    ]);
    if (emailAllowed && ipAllowed) {
      await deps.sendMagicLink(email);
    }
  }

  const elapsed = deps.now() - start;
  if (elapsed < MIN_RESPONSE_MS) {
    await deps.sleep(MIN_RESPONSE_MS - elapsed);
  }
}

/** Wires the real rate limiter's two scopes/limits — used by the server action. */
export function isAllowedByRateLimit(
  checkScope: (scope: "email" | "ip", key: string, limit: number) => Promise<boolean>,
): MagicLinkRequestDeps["isAllowed"] {
  return (scope, key) =>
    checkScope(
      scope,
      key,
      scope === "email" ? EMAIL_ATTEMPT_LIMIT_PER_HOUR : IP_ATTEMPT_LIMIT_PER_HOUR,
    );
}
