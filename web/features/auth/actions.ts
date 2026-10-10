"use server";

import { headers } from "next/headers";
import { AuthError } from "next-auth";

import { signIn } from "@/auth";
import { isAllowedByRateLimit, requestMagicLink } from "@/lib/auth/magicLinkRequest";
import { createPostgresSignInAttemptStore, isWithinRateLimit } from "@/lib/auth/rateLimit";

/**
 * Server action behind the login form (security phase 2, magic-link
 * hardening). One fixed message for every outcome — allowed, unprovisioned
 * email, or rate limited — so the response itself carries no signal for
 * `lib/auth/magicLinkRequest.ts`'s indistinguishability guarantee to protect.
 */
const CONFIRMATION_MESSAGE =
  "If that email has access, a sign-in link is on its way — check your inbox.";

/**
 * Resolves the caller's IP from the header Railway's edge appends.
 * `X-Forwarded-For` is a comma-separated hop list where each proxy appends
 * the address it received the connection from; Railway sits as the single
 * hop in front of `web`, so only the LAST entry was written by a proxy we
 * trust — anything earlier in the list is client-supplied and trivially
 * spoofable. Falls back to a fixed key (still rate limited, just as one
 * bucket) when the header is absent, e.g. local dev.
 */
function resolveClientIp(forwardedFor: string | null): string {
  if (!forwardedFor) return "unknown";
  const hops = forwardedFor
    .split(",")
    .map((hop) => hop.trim())
    .filter(Boolean);
  return hops.at(-1) ?? "unknown";
}

async function sendMagicLink(email: string, callbackUrl: string): Promise<void> {
  try {
    // redirect:false — a redirect thrown only on the "allowed" path would
    // itself be the distinguishing signal (redirect vs no redirect) this is
    // guarding against. The confirmation is rendered by LoginView instead,
    // identically for every outcome; redirectTo is still honored for where
    // the user lands once they actually click the link and verify.
    await signIn("resend", { email, redirectTo: callbackUrl, redirect: false });
  } catch (error) {
    if (!(error instanceof AuthError)) throw error;
  }
}

export async function requestMagicLinkAction(
  callbackUrl: string,
  _prevState: string | null,
  formData: FormData,
): Promise<string> {
  const email = String(formData.get("email") ?? "");
  const clientIp = resolveClientIp((await headers()).get("x-forwarded-for"));

  await requestMagicLink(email, clientIp, {
    isAllowed: isAllowedByRateLimit((scope, key, limit) =>
      isWithinRateLimit(createPostgresSignInAttemptStore(), scope, key, limit),
    ),
    sendMagicLink: (verifiedEmail) => sendMagicLink(verifiedEmail, callbackUrl),
    now: () => Date.now(),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  });

  return CONFIRMATION_MESSAGE;
}
