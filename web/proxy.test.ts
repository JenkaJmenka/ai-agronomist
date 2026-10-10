import type { ProxyMatcher } from "next/dist/build/analysis/get-page-static-info";
import * as nextPageStaticInfo from "next/dist/build/analysis/get-page-static-info";
import type { NextResponse } from "next/server";
import { describe, expect, it, vi } from "vitest";

import proxyHandler, { config } from "@/proxy";

/**
 * Regression coverage for the >10MB CSV upload corruption: Next 16's proxy
 * buffers every *matched* request body in memory with a hard 10MB cap and
 * silently truncates past it, which destroyed the multipart `file` part and
 * every field after it (`uploadFormData` writes `file` first), surfacing as a
 * FastAPI 422 with `tenant_id` and `file` both missing.
 *
 * The fix is the two `$`-anchored exclusions in the matcher below, so the
 * matcher itself is the thing under test: if someone edits that regex and
 * either upload path starts being matched again — or an ordinary route stops
 * being matched, losing its session/tenant stamp — these tests must fail.
 */

// proxy.ts imports `@/auth`, which builds a Postgres pool at module scope.
// Only the matcher and the handler's own logic are under test, so the
// Auth.js wrapper is stubbed to keep this a pure test with no DB wiring.
// Because `auth` is mocked to the identity function, `proxy.ts`'s default
// export below is literally its own inner `(req) => {...}` callback — real
// enough to call directly with a hand-built request.
vi.mock("@/auth", () => ({
  auth: (handler: unknown) => handler,
}));

/**
 * `getMiddlewareMatchers` is exported at runtime but missing from Next's
 * shipped type declarations, hence the narrowing cast. Using Next's own
 * path-to-regexp compilation (rather than re-implementing it here) is what
 * makes these assertions describe real request matching: the raw matcher
 * source is unanchored and compiles to something quite different.
 */
type MatcherCompiler = (matcher: string | string[], nextConfig: object) => ProxyMatcher[];

const { getMiddlewareMatchers } = nextPageStaticInfo as unknown as {
  getMiddlewareMatchers?: MatcherCompiler;
};

const UPLOAD_PATHS = ["/api/bff/inference/upload", "/api/bff/inference/upload-async"] as const;

function isMatchedByProxy(pathname: string): boolean {
  if (typeof getMiddlewareMatchers !== "function") {
    throw new Error(
      "next/dist/build/analysis/get-page-static-info no longer exports getMiddlewareMatchers — " +
        "this test can no longer compile the matcher the way the Next build does",
    );
  }
  return getMiddlewareMatchers(config.matcher, {}).some((matcher) =>
    new RegExp(matcher.regexp).test(pathname),
  );
}

describe("proxy matcher — upload exclusions", () => {
  it.each(UPLOAD_PATHS)("should not match %s (the proxy would truncate its body)", (pathname) => {
    expect(isMatchedByProxy(pathname)).toBe(false);
  });

  it("should still match a path that merely starts with an excluded upload path", () => {
    // The exclusions are `$`-anchored on purpose. Dropping the anchors would
    // silently take any future `upload-*` route out of the auth gate.
    expect(isMatchedByProxy("/api/bff/inference/upload-async-foo")).toBe(true);
    expect(isMatchedByProxy("/api/bff/inference/uploads")).toBe(true);
    expect(isMatchedByProxy("/api/bff/inference/upload/status")).toBe(true);
  });

  it("should match every other BFF route so the session tenant is still stamped", () => {
    const bffPaths = [
      "/api/bff/anomalies",
      "/api/bff/readings/window",
      "/api/bff/monitoring/metrics",
      "/api/bff/models/tenant_7/versions",
      "/api/bff/inference/predict",
      "/api/bff/live/events",
      "/api/bff/jobs/abc-123/events",
    ];

    for (const pathname of bffPaths) {
      expect(isMatchedByProxy(pathname), `${pathname} should be matched`).toBe(true);
    }
  });

  it("should exclude only the two upload paths out of the BFF surface", () => {
    const allBffPaths = [...UPLOAD_PATHS, "/api/bff/anomalies", "/api/bff/inference/predict"];

    const excluded = allBffPaths.filter((pathname) => !isMatchedByProxy(pathname));

    expect(excluded).toEqual([...UPLOAD_PATHS]);
  });
});

describe("proxy matcher — pre-existing matches are unchanged", () => {
  it("should match app pages, including the root", () => {
    for (const pathname of ["/", "/monitoring", "/login", "/operations"]) {
      expect(isMatchedByProxy(pathname), `${pathname} should be matched`).toBe(true);
    }
  });

  it("should match non-BFF API routes", () => {
    expect(isMatchedByProxy("/api/health")).toBe(true);
    expect(isMatchedByProxy("/api/auth/callback/resend")).toBe(true);
  });

  it("should not match Next's build output or the favicon", () => {
    for (const pathname of ["/_next/static/chunks/main.js", "/_next/image", "/favicon.ico"]) {
      expect(isMatchedByProxy(pathname), `${pathname} should be excluded`).toBe(false);
    }
  });

  it("should not match static image requests", () => {
    for (const pathname of ["/logo.svg", "/hero.png", "/photo.jpeg", "/anim.webp"]) {
      expect(isMatchedByProxy(pathname), `${pathname} should be excluded`).toBe(false);
    }
  });
});

/**
 * Regression coverage for the production "tenant undefined" bug: a session
 * missing `tenantId` (self-heal in `lib/auth/tenantClaim.ts` couldn't fill it
 * in) must never reach the point where `Headers.set` coerces it to the
 * literal string `"undefined"` and forwards it upstream. See `proxy.ts`'s
 * file header for the full incident writeup.
 *
 * `NextResponse.next({ request: { headers } })` doesn't expose the rewritten
 * *request* headers on the returned response object directly — Next encodes
 * them as `x-middleware-request-<key>` response headers instead (see
 * `next/dist/server/web/spec-extension/response.js`), so that's what these
 * assertions read to prove whether `x-tenant-id` was actually forwarded.
 */
const FORWARDED_TENANT_HEADER = "x-middleware-request-x-tenant-id";

interface FakeSession {
  user: { tenantId?: string; email?: string | null };
}

interface FakeCookie {
  readonly name: string;
  readonly value: string;
}

/** Just enough of `NextRequest` for `proxy.ts`'s handler to run against. */
function createFakeRequest(options: {
  pathname: string;
  auth?: FakeSession | null;
  cookies?: readonly FakeCookie[];
}): Parameters<typeof proxyHandler>[0] {
  const nextUrl = new URL(`https://example.com${options.pathname}`);
  const fakeRequest = {
    nextUrl,
    auth: options.auth ?? null,
    headers: new Headers(),
    cookies: { getAll: () => options.cookies ?? [] },
  };
  return fakeRequest as unknown as Parameters<typeof proxyHandler>[0];
}

function callProxy(options: Parameters<typeof createFakeRequest>[0]) {
  const request = createFakeRequest(options);
  // `proxy.ts`'s handler is synchronous; the `NextMiddleware` type it's
  // declared as also allows a `Promise`, hence the cast down to what it
  // actually returns here.
  return proxyHandler(request, {} as never) as NextResponse | undefined;
}

describe("proxy handler — fail-closed on a missing tenant", () => {
  it("never stamps x-tenant-id — let alone the literal string 'undefined' — for an authenticated session with no tenant", () => {
    const response = callProxy({
      pathname: "/api/bff/models/tenant_1/versions",
      auth: { user: { tenantId: undefined, email: "grower@example.com" } },
    });

    expect(response?.status).toBe(401);
    expect(response?.headers.get(FORWARDED_TENANT_HEADER)).toBeNull();
  });

  it("401s a /api/bff/* request when the session has no tenant, and clears the session cookie", () => {
    const response = callProxy({
      pathname: "/api/bff/anomalies",
      auth: { user: { tenantId: undefined, email: "grower@example.com" } },
      cookies: [{ name: "authjs.session-token", value: "opaque" }],
    });

    expect(response?.status).toBe(401);
    expect(response?.headers.get(FORWARDED_TENANT_HEADER)).toBeNull();
    const setCookie = response?.cookies.get("authjs.session-token");
    expect(setCookie?.value).toBe("");
  });

  it("redirects a page request to /login when the session has no tenant, and clears the session cookie", () => {
    const response = callProxy({
      pathname: "/monitoring",
      auth: { user: { tenantId: undefined, email: "grower@example.com" } },
      cookies: [{ name: "__Secure-authjs.session-token", value: "opaque" }],
    });

    expect(response?.status).toBe(307);
    expect(response?.headers.get("location")).toBe("https://example.com/login");
    const setCookie = response?.cookies.get("__Secure-authjs.session-token");
    expect(setCookie?.value).toBe("");
  });

  it("cannot loop: the redirect target is /login, which this same handler always allows regardless of session state", () => {
    const stillAuthenticatedNoTenant = callProxy({
      pathname: "/login",
      auth: { user: { tenantId: undefined, email: "grower@example.com" } },
    });
    expect(stillAuthenticatedNoTenant?.status ?? 200).not.toBe(307);

    const anonymous = callProxy({ pathname: "/login", auth: null });
    expect(anonymous?.status ?? 200).not.toBe(307);
  });

  it('never stamps x-tenant-id on a PUBLIC path either, for an authenticated session with no tenant (regression: decideAuthAction returns "allow" unconditionally for /login, /api/auth/*, /api/health, so the switch never early-returns here — only the final tenantId guard in proxy.ts stops the stamp)', () => {
    const noTenantSession = { user: { tenantId: undefined, email: "grower@example.com" } };

    for (const pathname of ["/login", "/api/auth/callback/resend", "/api/health"]) {
      const response = callProxy({ pathname, auth: noTenantSession });
      expect(
        response?.headers.get(FORWARDED_TENANT_HEADER),
        `${pathname} must not forward a tenant`,
      ).toBeNull();
    }
  });

  it("stamps the real tenant and leaves cookies untouched for a normal authenticated session", () => {
    const response = callProxy({
      pathname: "/api/bff/anomalies",
      auth: { user: { tenantId: "tenant_1", email: "grower@example.com" } },
      cookies: [{ name: "authjs.session-token", value: "opaque" }],
    });

    expect(response?.headers.get(FORWARDED_TENANT_HEADER)).toBe("tenant_1");
    expect(response?.headers.get("x-middleware-set-cookie")).toBeNull();
  });
});
