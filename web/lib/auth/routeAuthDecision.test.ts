import { describe, expect, it } from "vitest";

import { decideAuthAction, type SessionAuthState } from "./routeAuthDecision";

const ANONYMOUS: SessionAuthState = { isLoggedIn: false, hasTenant: false };
const LOGGED_IN_NO_TENANT: SessionAuthState = { isLoggedIn: true, hasTenant: false };
const LOGGED_IN_WITH_TENANT: SessionAuthState = { isLoggedIn: true, hasTenant: true };

describe("decideAuthAction", () => {
  it("allows /login for an anonymous caller", () => {
    expect(decideAuthAction("/login", ANONYMOUS)).toEqual({ kind: "allow" });
  });

  it("allows /api/auth/* for an anonymous caller (the sign-in flow itself)", () => {
    expect(decideAuthAction("/api/auth/callback/resend", ANONYMOUS)).toEqual({ kind: "allow" });
  });

  it("allows /api/health for an anonymous caller (Railway's healthcheck probe has no session)", () => {
    expect(decideAuthAction("/api/health", ANONYMOUS)).toEqual({ kind: "allow" });
  });

  it("still redirects the app's own root — a lesson learned from a real outage: '/' is not public", () => {
    // Railway's healthcheckPath was "/" before this fix; an anonymous probe
    // got redirected (307) instead of 200 and the deploy failed. Guards
    // against re-pointing the healthcheck back at "/" by accident.
    expect(decideAuthAction("/", ANONYMOUS)).toEqual({ kind: "redirect-to-login" });
  });

  it("401s an anonymous /api/bff/* request instead of redirecting", () => {
    expect(decideAuthAction("/api/bff/readings/window", ANONYMOUS)).toEqual({
      kind: "deny-unauthenticated-api",
    });
  });

  it("401s an anonymous BFF SSE request the same as any other BFF route", () => {
    expect(decideAuthAction("/api/bff/live/events", ANONYMOUS)).toEqual({
      kind: "deny-unauthenticated-api",
    });
    expect(decideAuthAction("/api/bff/jobs/abc-123/events", ANONYMOUS)).toEqual({
      kind: "deny-unauthenticated-api",
    });
  });

  it("redirects an anonymous request to any other app page", () => {
    expect(decideAuthAction("/monitoring", ANONYMOUS)).toEqual({ kind: "redirect-to-login" });
  });

  it("allows every route once logged in with a tenant", () => {
    expect(decideAuthAction("/monitoring", LOGGED_IN_WITH_TENANT)).toEqual({ kind: "allow" });
    expect(decideAuthAction("/api/bff/readings/window", LOGGED_IN_WITH_TENANT)).toEqual({
      kind: "allow",
    });
  });

  describe("authenticated but missing tenantId (regression: this must never resolve to 'allow')", () => {
    it("401s a /api/bff/* request rather than forwarding an absent tenant upstream", () => {
      expect(decideAuthAction("/api/bff/models/tenant_1/versions", LOGGED_IN_NO_TENANT)).toEqual({
        kind: "deny-missing-tenant-api",
      });
    });

    it("signs out and sends an app page to /login instead of forwarding an absent tenant", () => {
      expect(decideAuthAction("/monitoring", LOGGED_IN_NO_TENANT)).toEqual({
        kind: "signout-missing-tenant",
      });
    });

    it("still allows /login itself — landing there after signout-missing-tenant must not be re-evaluated", () => {
      expect(decideAuthAction("/login", LOGGED_IN_NO_TENANT)).toEqual({ kind: "allow" });
    });

    it("still allows the healthcheck and sign-in flow regardless of the missing tenant", () => {
      expect(decideAuthAction("/api/health", LOGGED_IN_NO_TENANT)).toEqual({ kind: "allow" });
      expect(decideAuthAction("/api/auth/callback/resend", LOGGED_IN_NO_TENANT)).toEqual({
        kind: "allow",
      });
    });
  });
});
