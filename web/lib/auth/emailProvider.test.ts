import { readFileSync } from "node:fs";
import path from "node:path";
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

  it("throws naming AUTH_RESEND_FROM when it is unset — never falls back to an unowned domain (regression for the ai-agronomist.app incident)", async () => {
    vi.stubEnv("AUTH_RESEND_KEY", "re_test_key_123");
    vi.stubEnv("AUTH_RESEND_FROM", undefined);
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    await expect(magicLinkProvider.sendVerificationRequest(verificationParams)).rejects.toThrow(
      /AUTH_RESEND_FROM/,
    );

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("uses the configured AUTH_RESEND_FROM as the sender — routing does not depend on NODE_ENV", async () => {
    // NODE_ENV under vitest is "test", not "production" — this is the exact
    // combination (non-production NODE_ENV, real key present) that the old
    // NODE_ENV-keyed check got wrong: it mocked regardless of the key. Here
    // the key alone must be enough to route to the real sender.
    expect(process.env.NODE_ENV).not.toBe("production");
    vi.stubEnv("AUTH_RESEND_KEY", "re_test_key_123");
    vi.stubEnv("AUTH_RESEND_FROM", "AI Agronomist <no-reply@example.com>");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    await magicLinkProvider.sendVerificationRequest(verificationParams);

    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const [url, requestInit] = vi.mocked(globalThis.fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://api.resend.com/emails");
    const body = JSON.parse(requestInit.body as string) as { from: string };
    expect(body.from).toBe("AI Agronomist <no-reply@example.com>");
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("logs the [auth:send-failed] marker and re-throws when Resend rejects the send", async () => {
    vi.stubEnv("AUTH_RESEND_KEY", "re_test_key_123");
    vi.stubEnv("AUTH_RESEND_FROM", "AI Agronomist <no-reply@example.com>");
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue({ ok: false, json: async () => ({ message: "domain not verified" }) }),
    );
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    await expect(magicLinkProvider.sendVerificationRequest(verificationParams)).rejects.toThrow(
      /Resend error/,
    );

    expect(errorSpy).toHaveBeenCalledOnce();
    const [errorMessage] = errorSpy.mock.calls[0] as [string];
    expect(errorMessage).toContain("[auth:send-failed]");
    expect(errorMessage).toContain(verificationParams.identifier);
  });

  it("no longer references the unowned ai-agronomist.app domain anywhere in the provider source", () => {
    const sourcePath = path.resolve(process.cwd(), "lib/auth/emailProvider.ts");
    const source = readFileSync(sourcePath, "utf-8");

    expect(source).not.toContain("ai-agronomist.app");
  });
});
