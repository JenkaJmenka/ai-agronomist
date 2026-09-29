/**
 * Pure logic behind `auth.ts`'s `jwt` callback (security phase 2, §1
 * addendum). Split out so the self-heal behaviour is unit-testable with a
 * fake lookup — no Postgres pool, no NextAuth wiring.
 *
 * The bug this fixes: Auth.js only passes `user` (and `trigger === "signIn"`)
 * to the `jwt` callback at sign-in. Every later call — a session refresh —
 * arrives with neither, so a token that ever lacked `tenantId` (provisioned
 * after first sign-in, or the claim added by a later deploy) kept lacking it
 * forever, even though `expires` kept rolling forward. `resolveTenantClaim`
 * backfills from `token.email` whenever the claim is missing, not just at
 * sign-in, and only then — once present, no further lookup runs.
 *
 * Scope limit, deliberate: if the lookup returns `null` the token is
 * returned unchanged. `signIn` in `auth.ts` is the only gate that denies a
 * sign-in outright; a mapping revoked after a token was already issued is
 * left for `proxy.ts` to fail closed on (see `lib/auth/routeAuthDecision.ts`),
 * not for this callback to newly invalidate.
 */

/** Matches `next-auth`'s `JWT` shape closely enough for this module's needs. */
export interface TokenWithTenant {
  readonly tenantId?: string;
  readonly email?: string | null;
  readonly [key: string]: unknown;
}

export interface TenantClaimDeps {
  /** Resolves the single tenant an email belongs to, or `null` if unmapped. */
  lookupTenantId(email: string): Promise<string | null>;
}

export interface ResolveTenantClaimParams {
  readonly token: TokenWithTenant;
  /** The email `next-auth` passes as `user.email` — only present at sign-in. */
  readonly signInEmail?: string | null;
  readonly isSignIn: boolean;
}

export async function resolveTenantClaim(
  { token, signInEmail, isSignIn }: ResolveTenantClaimParams,
  deps: TenantClaimDeps,
): Promise<TokenWithTenant> {
  if (isSignIn) {
    if (!signInEmail) return token;
    const tenantId = await deps.lookupTenantId(signInEmail);
    return tenantId ? { ...token, tenantId } : token;
  }

  if (token.tenantId || !token.email) return token;
  const tenantId = await deps.lookupTenantId(token.email);
  return tenantId ? { ...token, tenantId } : token;
}
