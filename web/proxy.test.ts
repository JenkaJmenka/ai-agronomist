import type { ProxyMatcher } from "next/dist/build/analysis/get-page-static-info";
import * as nextPageStaticInfo from "next/dist/build/analysis/get-page-static-info";
import { describe, expect, it, vi } from "vitest";

import { config } from "@/proxy";

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
// Only `config.matcher` is under test, so the Auth.js wrapper is stubbed to
// keep this a pure matching test with no DB or session wiring.
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
