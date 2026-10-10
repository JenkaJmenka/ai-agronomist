import { NextResponse } from "next/server";

/**
 * Railway healthcheck target (`.railway/railway.ts`'s `healthcheck`).
 * Deliberately its own route rather than reusing `/login`: a healthcheck
 * should only ever say "is the server answering requests" — it must not
 * start failing because of a login-page bug, an `AUTH_DATABASE_URL`/
 * `AUTH_RESEND_KEY` misconfiguration, or any other login-flow change. This
 * handler touches no session, no database, and no external service.
 *
 * Listed in `lib/auth/routeAuthDecision.ts`'s `PUBLIC_PATH_PREFIXES` so
 * `proxy.ts` never redirects an unauthenticated probe away from it — that
 * exact failure (redirect breaking the healthcheck) is why this route
 * exists; see the incident this fixed for the concrete 307 loop.
 */
export async function GET(): Promise<NextResponse> {
  return NextResponse.json({ status: "ok" });
}
