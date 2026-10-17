"use server";

import { redirect } from "next/navigation";

import { signOut } from "@/auth";

/**
 * Server Action backing `SignOutButton`. `signOut` (re-exported from
 * `auth.ts`, Auth.js v5) builds a POST request to `/api/auth/signout` and
 * runs it in-process against the same core handler
 * `app/api/auth/[...nextauth]/route.ts` exposes over HTTP — see
 * `node_modules/next-auth/lib/actions.js`. `redirect: false` keeps it from
 * throwing Next's internal redirect control-flow signal itself, so a
 * `catch` here only ever sees a genuine failure (cookie jar write, etc.);
 * the subsequent `redirect()` call is ours. `/api/auth/` is a public path in
 * `routeAuthDecision.ts`, so `proxy.ts` never gates this request on session
 * state the way it does for the upload routes.
 */
export async function signOutAction(): Promise<{ readonly ok: false } | undefined> {
  try {
    await signOut({ redirectTo: "/login", redirect: false });
  } catch (error) {
    console.error("Sign-out failed", error);
    return { ok: false };
  }
  redirect("/login");
}
