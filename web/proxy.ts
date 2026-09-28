/**
 * Next 16 Proxy (renamed from `middleware.ts` — see
 * `node_modules/next/dist/docs/.../file-conventions/proxy.md`). Runs on the
 * Node.js runtime by default here, which is why `auth.ts`'s full config
 * (Postgres adapter included) can be imported directly instead of needing
 * the edge-safe split config older Auth.js/Next guides recommend.
 *
 * Security phase 2, §2 — fail closed everywhere:
 *   - anonymous page request  -> redirect to /login
 *   - anonymous /api/bff/*    -> 401 (including the two SSE routes)
 *
 * The session's tenant is stamped onto the request as `x-tenant-id` here —
 * the one place in the app that turns "logged in" into "which tenant" — so
 * the BFF routes never have to trust a client-supplied tenant again.
 */

import { NextResponse } from "next/server";

import { auth } from "@/auth";
import { decideAuthAction } from "@/lib/auth/routeAuthDecision";

export default auth((req) => {
  const { pathname, origin } = req.nextUrl;
  const decision = decideAuthAction(pathname, !!req.auth);

  switch (decision.kind) {
    case "allow":
      break;
    case "deny-unauthenticated-api":
      return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
    case "redirect-to-login": {
      const loginUrl = new URL("/login", origin);
      loginUrl.searchParams.set("callbackUrl", pathname);
      return NextResponse.redirect(loginUrl);
    }
  }

  if (!req.auth) return NextResponse.next();

  // Drop any client-sent x-tenant-id before setting the real one — a
  // caller cannot smuggle a value through this header.
  const requestHeaders = new Headers(req.headers);
  requestHeaders.delete("x-tenant-id");
  requestHeaders.set("x-tenant-id", req.auth.user.tenantId);

  return NextResponse.next({ request: { headers: requestHeaders } });
});

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
