import { describe, expect, it, vi } from "vitest";

import { type MagicLinkRequestDeps, MIN_RESPONSE_MS, requestMagicLink } from "./magicLinkRequest";

function createDeps(overrides: Partial<MagicLinkRequestDeps> = {}): {
  deps: MagicLinkRequestDeps;
  sendMagicLink: ReturnType<typeof vi.fn>;
  clock: { value: number };
} {
  const clock = { value: 0 };
  const sendMagicLink = vi.fn(async () => {});
  const deps: MagicLinkRequestDeps = {
    isAllowed: async () => true,
    sendMagicLink,
    now: () => clock.value,
    // Deterministic "sleep" — just advances the fake clock instead of waiting.
    sleep: async (ms) => {
      clock.value += ms;
    },
    ...overrides,
  };
  return { deps, sendMagicLink, clock };
}

describe("requestMagicLink", () => {
  it("sends the link and resolves (no thrown error, no return value) for an allowed request", async () => {
    const { deps, sendMagicLink } = createDeps();
    await expect(
      requestMagicLink("known@example.com", "203.0.113.1", deps),
    ).resolves.toBeUndefined();
    expect(sendMagicLink).toHaveBeenCalledExactlyOnceWith("known@example.com");
  });

  it("never calls sendMagicLink for an email the rate limiter or signIn callback would deny", async () => {
    // isAllowed=false models both "unprovisioned email" (handled inside
    // sendMagicLink normally, but the rate limiter is a separate, earlier
    // gate) and "over the rate limit" — either way, nothing is sent.
    const { deps, sendMagicLink } = createDeps({ isAllowed: async () => false });
    await expect(
      requestMagicLink("unknown@example.com", "203.0.113.1", deps),
    ).resolves.toBeUndefined();
    expect(sendMagicLink).not.toHaveBeenCalled();
  });

  it("resolves with the exact same value (undefined) whether or not the request was allowed", async () => {
    const { deps: allowedDeps } = createDeps({ isAllowed: async () => true });
    const { deps: deniedDeps } = createDeps({ isAllowed: async () => false });
    const allowedResult = await requestMagicLink("a@example.com", "203.0.113.1", allowedDeps);
    const deniedResult = await requestMagicLink("b@example.com", "203.0.113.1", deniedDeps);
    // Both are `undefined` — there is no field to diff, by construction.
    expect(allowedResult).toBe(deniedResult);
  });

  it("pads an allowed request up to MIN_RESPONSE_MS when the work finishes early", async () => {
    const { deps, clock } = createDeps({
      now: () => clock.value,
      sendMagicLink: async () => {
        clock.value += 5; // the "send" itself is fast
      },
    });
    const before = deps.now();
    await requestMagicLink("known@example.com", "203.0.113.1", deps);
    expect(deps.now() - before).toBeGreaterThanOrEqual(MIN_RESPONSE_MS);
  });

  it("pads a denied request up to MIN_RESPONSE_MS too — same floor, not just the allowed path", async () => {
    const { deps, clock } = createDeps({ isAllowed: async () => false, now: () => clock.value });
    const before = deps.now();
    await requestMagicLink("unknown@example.com", "203.0.113.1", deps);
    expect(deps.now() - before).toBeGreaterThanOrEqual(MIN_RESPONSE_MS);
  });

  it("does not pad further when the work already took longer than the floor", async () => {
    const { deps, clock } = createDeps();
    const slowSend = vi.fn(async () => {
      clock.value += MIN_RESPONSE_MS + 50;
    });
    const slowDeps: MagicLinkRequestDeps = { ...deps, sendMagicLink: slowSend };
    const before = slowDeps.now();
    await requestMagicLink("known@example.com", "203.0.113.1", slowDeps);
    // Slow work isn't slowed down further — no extra sleep is layered on top.
    expect(slowDeps.now() - before).toBe(MIN_RESPONSE_MS + 50);
    expect(slowSend).toHaveBeenCalledOnce();
  });

  it("normalizes email case/whitespace before checking or sending, and treats blank input like any other denial", async () => {
    const { deps, sendMagicLink } = createDeps();
    await requestMagicLink("  Mixed@Example.com  ", "203.0.113.1", deps);
    expect(sendMagicLink).toHaveBeenCalledExactlyOnceWith("mixed@example.com");

    const { deps: blankDeps, sendMagicLink: blankSend } = createDeps();
    await expect(requestMagicLink("   ", "203.0.113.1", blankDeps)).resolves.toBeUndefined();
    expect(blankSend).not.toHaveBeenCalled();
  });
});
