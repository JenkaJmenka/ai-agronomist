/**
 * Fixed Auth.js secret for the e2e run only (security phase 2, §2). E2E is
 * hermetic — no real Postgres, no real Resend — so `global-setup.ts` mints a
 * session cookie directly instead of driving the magic-link UI; this secret
 * has to match between that minting step and the `pnpm start` server
 * (`playwright.config.ts` passes it into `webServer.env`) or the server
 * rejects the cookie as an invalid signature.
 */
export const E2E_AUTH_SECRET = "e2e-test-secret-do-not-use-in-production";
