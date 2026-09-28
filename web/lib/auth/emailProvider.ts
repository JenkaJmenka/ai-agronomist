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
 */

import type { EmailConfig } from "next-auth/providers/email";
import Resend from "next-auth/providers/resend";

const AUTH_RESEND_FROM =
  process.env.AUTH_RESEND_FROM ?? "AI Agronomist <no-reply@ai-agronomist.app>";

function isEmailSenderMocked(): boolean {
  return process.env.NODE_ENV !== "production";
}

const resendProvider = Resend({
  apiKey: process.env.AUTH_RESEND_KEY ?? "unset-outside-production",
  from: AUTH_RESEND_FROM,
});

export const magicLinkProvider: EmailConfig = {
  ...resendProvider,
  async sendVerificationRequest(params) {
    if (isEmailSenderMocked()) {
      console.info(`[auth:mock-email] magic link for ${params.identifier}: ${params.url}`);
      return;
    }
    await resendProvider.sendVerificationRequest(params);
  },
};
