import { describe, expect, it, vi } from "vitest";

import { MAGIC_LINK_MAX_AGE_SECONDS, magicLinkProvider } from "./emailProvider";

describe("magicLinkProvider", () => {
  it("sets the verification token lifetime to 15 minutes, not Auth.js's 24-hour default", () => {
    expect(MAGIC_LINK_MAX_AGE_SECONDS).toBe(15 * 60);
    expect(magicLinkProvider.maxAge).toBe(15 * 60);
  });

  it("never calls the real Resend API outside production (dev/test mock)", async () => {
    // NODE_ENV is "test" under vitest — isEmailSenderMocked() should short-circuit.
    expect(process.env.NODE_ENV).not.toBe("production");
    const infoSpy = vi.spyOn(console, "info").mockImplementation(() => {});
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    await magicLinkProvider.sendVerificationRequest({
      identifier: "someone@example.com",
      url: "https://example.com/api/auth/callback/resend?token=abc",
      expires: new Date(),
      token: "abc",
      provider: magicLinkProvider,
      theme: {},
      request: new Request("https://example.com"),
    });

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(infoSpy).toHaveBeenCalledOnce();
    infoSpy.mockRestore();
    fetchSpy.mockRestore();
  });
});
