/**
 * Magic-link sign-in via Resend (security phase 2, §1 — provider decided by
 * the user, no alternative to evaluate). Uses the provider built into
 * `next-auth` (`next-auth/providers/resend`), which talks to a plain HTTPS
 * API — no SMTP config, no nodemailer.
 *
 * `AUTH_RESEND_KEY` (not `RESEND_API_KEY`) matches Auth.js's own env var
 * convention for this provider: `setEnvDefaults` in `@auth/core` auto-fills
 * an email-type provider's `apiKey` from `AUTH_{PROVIDER_ID}_KEY` — for the
 * "resend" provider id, that's `AUTH_RESEND_KEY`. There's no equivalent
 * convention for the from-address, so `AUTH_RESEND_FROM` is this app's own
 * choice, named to match that prefix.
 *
 * `maxAge: 15 * 60` (magic-link hardening, item a) shortens the link's
 * lifetime from Auth.js's 24-hour default to 15 minutes — `@auth/core` reads
 * this value to set the verification token's `expires` column at creation
 * (`send-token.js`: `expires = Date.now() + (provider.maxAge ?? ONE_DAY) *
 * 1000`) and rejects the token as expired past that point
 * (`callback/index.js`'s `hasInvite && invite.expires.valueOf() < Date.now()`
 * check). Single-use needs no code here: that same callback path calls
 * `adapter.useVerificationToken` — which `@auth/pg-adapter` implements as a
 * `delete ... returning ...` — *before* checking expiry, so a token is
 * removed from `auth.verification_token` on its first use (valid or
 * expired) and a replay finds no row at all. Verified against a live
 * Postgres, not just read from source — see the report to team-lead.
 *
 * --- Mock/real routing (post-incident redesign) ---
 * A production deploy once shipped with `NODE_ENV` unset on the `web`
 * service. The previous version of this file keyed the mock decision off
 * `NODE_ENV !== "production"`, so the unset var silently took the mock
 * branch: a verification token was created, the UI showed the normal
 * success confirmation (by design, per the anti-enumeration work — a mocked
 * send is meant to look identical to a real one), and no email ever went
 * out. No error, no warning, nothing to alert on.
 *
 * The fix is to never consult `NODE_ENV` for this decision at all. The only
 * question that matters is "can we send" — so the branch condition *is*
 * `AUTH_RESEND_KEY` presence, checked fresh on every call rather than cached
 * at module load. A deployment holding a real key cannot silently take the
 * mock branch: there is no other flag, no other variable, and no fallback
 * string standing in for a missing key that could make a broken config look
 * configured. When the key genuinely is absent, `sendVerificationRequest`
 * logs at `warn` (not `info`) and says plainly that nothing was sent.
 */

import type { EmailConfig } from "next-auth/providers/email";
import Resend from "next-auth/providers/resend";

const AUTH_RESEND_FROM =
  process.env.AUTH_RESEND_FROM ?? "AI Agronomist <no-reply@ai-agronomist.app>";

/** 15 minutes, in seconds — `EmailConfig.maxAge` is seconds, not ms. */
export const MAGIC_LINK_MAX_AGE_SECONDS = 15 * 60;

export const magicLinkProvider: EmailConfig = {
  id: "resend",
  type: "email",
  name: "Resend",
  from: AUTH_RESEND_FROM,
  maxAge: MAGIC_LINK_MAX_AGE_SECONDS,
  async sendVerificationRequest(params) {
    const apiKey = process.env.AUTH_RESEND_KEY;
    if (!apiKey) {
      console.warn(
        `[auth:email-not-sent] AUTH_RESEND_KEY is not set — no magic-link email was sent ` +
          `to ${params.identifier}. Set AUTH_RESEND_KEY to enable real delivery. ` +
          `Link (not delivered anywhere, dev/test use only): ${params.url}`,
      );
      return;
    }
    await Resend({ apiKey, from: AUTH_RESEND_FROM }).sendVerificationRequest(params);
  },
};
