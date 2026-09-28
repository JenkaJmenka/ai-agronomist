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
 * Dev and test never call the real Resend API: `sendVerificationRequest` is
 * overridden to log the magic link instead whenever `NODE_ENV !== "production"`.
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
 */

import type { EmailConfig } from "next-auth/providers/email";
import Resend from "next-auth/providers/resend";

const AUTH_RESEND_FROM =
  process.env.AUTH_RESEND_FROM ?? "AI Agronomist <no-reply@ai-agronomist.app>";

/** 15 minutes, in seconds — `EmailConfig.maxAge` is seconds, not ms. */
export const MAGIC_LINK_MAX_AGE_SECONDS = 15 * 60;

function isEmailSenderMocked(): boolean {
  return process.env.NODE_ENV !== "production";
}

const resendProvider = Resend({
  apiKey: process.env.AUTH_RESEND_KEY ?? "unset-outside-production",
  from: AUTH_RESEND_FROM,
});

export const magicLinkProvider: EmailConfig = {
  ...resendProvider,
  maxAge: MAGIC_LINK_MAX_AGE_SECONDS,
  async sendVerificationRequest(params) {
    if (isEmailSenderMocked()) {
      console.info(`[auth:mock-email] magic link for ${params.identifier}: ${params.url}`);
      return;
    }
    await resendProvider.sendVerificationRequest(params);
  },
};
