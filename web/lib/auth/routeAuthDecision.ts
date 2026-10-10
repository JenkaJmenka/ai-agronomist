/**
 * Pure routing decision for `proxy.ts` (security phase 2, §2) — kept
 * separate from the Auth.js/Next.js wiring so the fail-closed rule ("every
 * app page and every /api/bff/* route needs a session") is unit-testable
 * without a request/response object.
 *
 * `/api/health` is here for the same reason `/login` and `/api/auth/` are:
 * Railway's healthcheck probe has no session, and a probe that gets
 * redirected instead of a 200 fails the deploy (`.railway/railway.ts`'s
 * `healthcheck`).
 *
 * `hasTenant` is a second, independent axis from `isLoggedIn`: a session can
 * be authenticated yet still lack `tenantId` (see `lib/auth/tenantClaim.ts`
 * for when the self-heal backfill can't fill it in — a revoked
 * `user_tenants` mapping). `proxy.ts` must never forward that session's
 * request as if it had a tenant, so that state gets its own decisions rather
 * than falling into `"allow"`.
 */

export type AuthDecision =
  | { readonly kind: "allow" }
  | { readonly kind: "deny-unauthenticated-api" }
  | { readonly kind: "redirect-to-login" }
  | { readonly kind: "deny-missing-tenant-api" }
  | { readonly kind: "signout-missing-tenant" };

export interface SessionAuthState {
  readonly isLoggedIn: boolean;
  readonly hasTenant: boolean;
}

const PUBLIC_PATH_PREFIXES = ["/login", "/api/auth/", "/api/health"] as const;
const BFF_PATH_PREFIX = "/api/bff/";

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATH_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function isBffPath(pathname: string): boolean {
  return pathname.startsWith(BFF_PATH_PREFIX);
}

export function decideAuthAction(pathname: string, authState: SessionAuthState): AuthDecision {
  if (isPublicPath(pathname)) return { kind: "allow" };

  if (!authState.isLoggedIn) {
    return isBffPath(pathname)
      ? { kind: "deny-unauthenticated-api" }
      : { kind: "redirect-to-login" };
  }

  if (!authState.hasTenant) {
    // `signout-missing-tenant` cannot loop back here: it clears the session
    // cookie, so the very next request from this browser arrives with
    // `isLoggedIn: false` and takes the ordinary `redirect-to-login` branch
    // above instead — and `/login` itself is a public path regardless of
    // login state, so landing there is never itself re-evaluated against
    // this branch either.
    return isBffPath(pathname)
      ? { kind: "deny-missing-tenant-api" }
      : { kind: "signout-missing-tenant" };
  }

  return { kind: "allow" };
}
