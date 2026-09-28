import { describe, expect, it } from "vitest";

import { decideAuthAction } from "./routeAuthDecision";

describe("decideAuthAction", () => {
  it("allows /login for an anonymous caller", () => {
    expect(decideAuthAction("/login", false)).toEqual({ kind: "allow" });
  });

  it("allows /api/auth/* for an anonymous caller (the sign-in flow itself)", () => {
    expect(decideAuthAction("/api/auth/callback/resend", false)).toEqual({ kind: "allow" });
  });

  it("401s an anonymous /api/bff/* request instead of redirecting", () => {
    expect(decideAuthAction("/api/bff/readings/window", false)).toEqual({
      kind: "deny-unauthenticated-api",
    });
  });

  it("401s an anonymous BFF SSE request the same as any other BFF route", () => {
    expect(decideAuthAction("/api/bff/live/events", false)).toEqual({
      kind: "deny-unauthenticated-api",
    });
    expect(decideAuthAction("/api/bff/jobs/abc-123/events", false)).toEqual({
      kind: "deny-unauthenticated-api",
    });
  });

  it("redirects an anonymous request to any other app page", () => {
    expect(decideAuthAction("/monitoring", false)).toEqual({ kind: "redirect-to-login" });
    expect(decideAuthAction("/", false)).toEqual({ kind: "redirect-to-login" });
  });

  it("allows every route once logged in", () => {
    expect(decideAuthAction("/monitoring", true)).toEqual({ kind: "allow" });
    expect(decideAuthAction("/api/bff/readings/window", true)).toEqual({ kind: "allow" });
  });
});
