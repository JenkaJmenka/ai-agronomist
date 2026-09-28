import { afterEach, describe, expect, it, vi } from "vitest";

import { MAGIC_LINK_MAX_AGE_SECONDS, magicLinkProvider } from "./emailProvider";

const verificationParams = {
  identifier: "someone@example.com",
  url: "https://example.com/api/auth/callback/resend?token=abc",
  expires: new Date(),
  token: "abc",
  provider: magicLinkProvider,
  theme: {},
  request: new Request("https://example.com"),
};

describe("magicLinkProvider", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("sets the verification token lifetime to 15 minutes, not Auth.js's 24-hour default", () => {
    expect(MAGIC_LINK_MAX_AGE_SECONDS).toBe(15 * 60);
    expect(magicLinkProvider.maxAge).toBe(15 * 60);
  });

  it("mocks and warns when AUTH_RESEND_KEY is absent — never calls fetch (regression: this must stay true with no env vars set, which is how every test in this suite runs)", async () => {
    vi.stubEnv("AUTH_RESEND_KEY", undefined);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    await magicLinkProvider.sendVerificationRequest(verificationParams);

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalledOnce();
    const [warnMessage] = warnSpy.mock.calls[0] as [string];
    expect(warnMessage).toContain("no magic-link email was sent");
  });

  it("uses the real Resend sender when AUTH_RESEND_KEY is present — routing does not depend on NODE_ENV", async () => {
    // NODE_ENV under vitest is "test", not "production" — this is the exact
    // combination (non-production NODE_ENV, real key present) that the old
    // NODE_ENV-keyed check got wrong: it mocked regardless of the key. Here
    // the key alone must be enough to route to the real sender.
    expect(process.env.NODE_ENV).not.toBe("production");
    vi.stubEnv("AUTH_RESEND_KEY", "re_test_key_123");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    await magicLinkProvider.sendVerificationRequest(verificationParams);

    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const [url] = vi.mocked(globalThis.fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://api.resend.com/emails");
    expect(warnSpy).not.toHaveBeenCalled();
  });
});
