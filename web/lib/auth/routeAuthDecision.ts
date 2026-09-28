/**
 * Pure routing decision for `proxy.ts` (security phase 2, §2) — kept
 * separate from the Auth.js/Next.js wiring so the fail-closed rule ("every
 * app page and every /api/bff/* route needs a session") is unit-testable
 * without a request/response object.
 */

export type AuthDecision =
  | { readonly kind: "allow" }
  | { readonly kind: "deny-unauthenticated-api" }
  | { readonly kind: "redirect-to-login" };

const PUBLIC_PATH_PREFIXES = ["/login", "/api/auth/"] as const;
const BFF_PATH_PREFIX = "/api/bff/";

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATH_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function isBffPath(pathname: string): boolean {
  return pathname.startsWith(BFF_PATH_PREFIX);
}

export function decideAuthAction(pathname: string, isLoggedIn: boolean): AuthDecision {
  if (isPublicPath(pathname)) return { kind: "allow" };
  if (isLoggedIn) return { kind: "allow" };
  return isBffPath(pathname) ? { kind: "deny-unauthenticated-api" } : { kind: "redirect-to-login" };
}
