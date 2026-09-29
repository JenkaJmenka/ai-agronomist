/**
 * Module augmentation: every session carries the signed-in user's single
 * tenant (security phase 2, §1) — resolved once at sign-in from
 * `user_tenants` (see `lib/auth/db.ts`) and never re-derived from anything
 * the browser sends.
 *
 * `tenantId` is optional, not `string`: a token can still lack the claim —
 * self-heals on the next refresh (`lib/auth/tenantClaim.ts`), except when the
 * email's `user_tenants` mapping has been revoked out from under a live
 * session, which the backfill deliberately leaves alone (see `auth.ts`).
 * `proxy.ts` fails closed on that gap rather than forwarding an absent
 * tenant upstream — see its `signout-missing-tenant` / `deny-missing-tenant-api`
 * handling — so every other reader in the app may still assume a page it
 * reached already has a real tenant.
 *
 * The JWT payload is intentionally NOT augmented the same way: `next-auth`'s
 * `JWT` type is a bare `export *` of `@auth/core`'s (a package pnpm doesn't
 * hoist to a resolvable path here since it's not a direct dependency), so
 * `auth.ts` reads/writes the token's `tenantId` through a narrow, local type
 * assertion instead of relying on interface merging.
 */

import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    user: DefaultSession["user"] & {
      tenantId?: string;
    };
  }
}
