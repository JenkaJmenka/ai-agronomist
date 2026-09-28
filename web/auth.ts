/**
 * Auth.js v5 config (security phase 2, §1). Email magic-link is the only
 * sign-in method — there is no password and no self-serve sign-up: an
 * operator's email must already have a `user_tenants` row (provisioned out
 * of band, see `lib/auth/schema.sql`) or sign-in is denied outright, before
 * any mail is sent.
 *
 * Session strategy is deliberately `"jwt"` even though an adapter is
 * configured: Next 16's Proxy defaults to the Node.js runtime (see
 * `proxy.ts`), so a DB-backed session would work too, but keeping sign-in
 * cheap (no DB round trip per request) needs no correctness trade-off here —
 * the adapter is still required for the Email provider's user/verification-
 * token persistence regardless of session strategy.
 */

import NextAuth from "next-auth";
import PostgresAdapter from "@auth/pg-adapter";

import { getAuthPool, getTenantIdForEmail } from "@/lib/auth/db";
import { magicLinkProvider } from "@/lib/auth/emailProvider";

/** See the note in `lib/auth/types.d.ts` on why this isn't a type augmentation. */
interface TokenWithTenant {
  tenantId?: string;
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  adapter: PostgresAdapter(getAuthPool()),
  providers: [magicLinkProvider],
  session: { strategy: "jwt" },
  pages: { signIn: "/login" },
  // Railway terminates TLS and forwards standard proxy headers; without
  // this Auth.js rejects the forwarded host as untrusted.
  trustHost: true,
  callbacks: {
    async signIn({ user }) {
      if (!user.email) return false;
      const tenantId = await getTenantIdForEmail(user.email);
      // Fail closed: an email with no provisioned tenant never gets a
      // magic link, and never completes sign-in if it somehow had one.
      return tenantId !== null;
    },
    async jwt({ token, user, trigger }) {
      if (trigger === "signIn" && user?.email) {
        const tenantId = await getTenantIdForEmail(user.email);
        if (tenantId) return { ...token, tenantId } satisfies TokenWithTenant;
      }
      return token;
    },
    async session({ session, token }) {
      const tenantId = (token as TokenWithTenant).tenantId;
      if (!tenantId) return session;
      return { ...session, user: { ...session.user, tenantId } };
    },
  },
});
