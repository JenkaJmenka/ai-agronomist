/**
 * Module augmentation: every session carries the signed-in user's single
 * tenant (security phase 2, §1) — resolved once at sign-in from
 * `user_tenants` (see `lib/auth/db.ts`) and never re-derived from anything
 * the browser sends.
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
      tenantId: string;
    };
  }
}
