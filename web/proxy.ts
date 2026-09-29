/**
 * Next 16 Proxy (renamed from `middleware.ts` — see
 * `node_modules/next/dist/docs/.../file-conventions/proxy.md`). Runs on the
 * Node.js runtime by default here, which is why `auth.ts`'s full config
 * (Postgres adapter included) can be imported directly instead of needing
 * the edge-safe split config older Auth.js/Next guides recommend.
 *
 * Security phase 2, §2 — fail closed everywhere:
 *   - anonymous page request        -> redirect to /login
 *   - anonymous /api/bff/*          -> 401 (including the two SSE routes)
 *   - authenticated, no tenantId    -> page: sign out + redirect to /login;
 *                                       /api/bff/*: 401 (see below)
 *
 * The session's tenant is stamped onto the request as `x-tenant-id` here —
 * the one place in the app that turns "logged in" into "which tenant" — so
 * the BFF routes never have to trust a client-supplied tenant again. A
 * session can be authenticated yet still have no `tenantId` (self-heal in
 * `lib/auth/tenantClaim.ts` couldn't fill it in, e.g. a revoked
 * `user_tenants` mapping); `Headers.set` coerces `undefined` to the literal
 * string `"undefined"`, so without the guard below that state used to get
 * forwarded upstream as tenant `"undefined"` instead of being rejected here.
 * The header is only ever set when `tenantId` is a real string, and every
 * other branch below returns before reaching that point.
 *
 * `/api/bff/inference/upload` and `/api/bff/inference/upload-async` are
 * excluded from the matcher below: Next 16's proxy buffers every matched
 * request body in memory with a hard 10MB cap and silently truncates
 * anything larger (`proxyClientMaxBodySize` docs), which corrupts CSV
 * uploads over that size. Those two routes resolve the session and stamp
 * the tenant themselves — see `app/api/bff/_lib/uploadRouteHandler.ts`.
 */

import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { auth } from "@/auth";
import { decideAuthAction } from "@/lib/auth/routeAuthDecision";

/**
 * Every chunk Auth.js may split the session JWT into shares this substring
 * (`authjs.session-token`, `__Secure-authjs.session-token`,
 * `authjs.session-token.0`, ...) — matching on it rather than one exact name
 * clears the cookie regardless of the secure-prefix or chunking the library
 * picks, without hardcoding either.
 */
const SESSION_COOKIE_NAME_FRAGMENT = "authjs.session-token";

function clearSessionCookies(response: NextResponse, request: NextRequest): void {
  for (const cookie of request.cookies.getAll()) {
    if (cookie.name.includes(SESSION_COOKIE_NAME_FRAGMENT)) {
      response.cookies.delete(cookie.name);
    }
  }
}

export default auth((req) => {
  const { pathname, origin } = req.nextUrl;
  const decision = decideAuthAction(pathname, {
    isLoggedIn: !!req.auth,
    hasTenant: !!req.auth?.user.tenantId,
  });

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
    case "deny-missing-tenant-api": {
      const response = NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
      clearSessionCookies(response, req);
      return response;
    }
    case "signout-missing-tenant": {
      // Redirecting an authenticated caller to /login cannot loop: /login is
      // "allow" for every session state (see routeAuthDecision.ts), and
      // clearing the cookie here means the *next* request from this browser
      // arrives fully anonymous, taking the ordinary redirect-to-login
      // branch instead of ever re-entering this one.
      const response = NextResponse.redirect(new URL("/login", origin));
      clearSessionCookies(response, req);
      return response;
    }
  }

  // Drop any client-sent x-tenant-id before setting the real one — a caller
  // cannot smuggle a value through this header. Only ever set when a real
  // tenant is present: `decideAuthAction` already routed every other session
  // state (anonymous, or authenticated-without-tenant) to a return above, but
  // this guard is the actual thing preventing "undefined" from being
  // forwarded, not the switch — see the file header.
  const tenantId = req.auth?.user.tenantId;
  if (!tenantId) return NextResponse.next();

  const requestHeaders = new Headers(req.headers);
  requestHeaders.delete("x-tenant-id");
  requestHeaders.set("x-tenant-id", tenantId);

  return NextResponse.next({ request: { headers: requestHeaders } });
});

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|api/bff/inference/upload$|api/bff/inference/upload-async$|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
