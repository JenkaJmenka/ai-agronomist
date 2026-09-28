import { describe, expect, it } from "vitest";

import {
  EMAIL_ATTEMPT_LIMIT_PER_HOUR,
  IP_ATTEMPT_LIMIT_PER_HOUR,
  isWithinRateLimit,
  type SignInAttemptScope,
  type SignInAttemptStore,
} from "./rateLimit";

/** In-memory stand-in for `auth.signin_attempts` — no test here touches Postgres. */
function createFakeStore(): SignInAttemptStore {
  const rows: { scope: SignInAttemptScope; key: string; timestamp: number }[] = [];
  return {
    async pruneExpired(scope, key, cutoff) {
      for (let i = rows.length - 1; i >= 0; i -= 1) {
        const row = rows[i];
        if (row.scope === scope && row.key === key && row.timestamp <= cutoff) {
          rows.splice(i, 1);
        }
      }
    },
    async count(scope, key) {
      return rows.filter((row) => row.scope === scope && row.key === key).length;
    },
    async record(scope, key, timestamp) {
      rows.push({ scope, key, timestamp });
    },
  };
}

describe("isWithinRateLimit", () => {
  it("allows attempts up to the limit and denies the one after", async () => {
    const store = createFakeStore();
    const results: boolean[] = [];
    for (let i = 0; i < EMAIL_ATTEMPT_LIMIT_PER_HOUR + 1; i += 1) {
      results.push(
        await isWithinRateLimit(store, "email", "a@example.com", EMAIL_ATTEMPT_LIMIT_PER_HOUR),
      );
    }
    expect(results).toEqual([true, true, true, true, true, false]);
  });

  it("uses the IP limit independently from the email limit", async () => {
    const store = createFakeStore();
    for (let i = 0; i < IP_ATTEMPT_LIMIT_PER_HOUR; i += 1) {
      expect(await isWithinRateLimit(store, "ip", "203.0.113.1", IP_ATTEMPT_LIMIT_PER_HOUR)).toBe(
        true,
      );
    }
    expect(await isWithinRateLimit(store, "ip", "203.0.113.1", IP_ATTEMPT_LIMIT_PER_HOUR)).toBe(
      false,
    );
    // A different email under the same IP is a separate counter entirely.
    expect(
      await isWithinRateLimit(store, "email", "b@example.com", EMAIL_ATTEMPT_LIMIT_PER_HOUR),
    ).toBe(true);
  });

  it("does not count an attempt outside the 1-hour window", async () => {
    const store = createFakeStore();
    const hourMs = 60 * 60 * 1000;
    let clock = 0;
    const now = () => clock;

    // Fill the limit at t=0.
    for (let i = 0; i < EMAIL_ATTEMPT_LIMIT_PER_HOUR; i += 1) {
      expect(
        await isWithinRateLimit(store, "email", "c@example.com", EMAIL_ATTEMPT_LIMIT_PER_HOUR, now),
      ).toBe(true);
    }
    expect(
      await isWithinRateLimit(store, "email", "c@example.com", EMAIL_ATTEMPT_LIMIT_PER_HOUR, now),
    ).toBe(false);

    // Just over an hour later, the old attempts have aged out.
    clock = hourMs + 1;
    expect(
      await isWithinRateLimit(store, "email", "c@example.com", EMAIL_ATTEMPT_LIMIT_PER_HOUR, now),
    ).toBe(true);
  });

  it("keys email and ip scopes independently even with the same key string", async () => {
    const store = createFakeStore();
    const key = "shared@example.com";
    for (let i = 0; i < EMAIL_ATTEMPT_LIMIT_PER_HOUR; i += 1) {
      await isWithinRateLimit(store, "email", key, EMAIL_ATTEMPT_LIMIT_PER_HOUR);
    }
    expect(await isWithinRateLimit(store, "email", key, EMAIL_ATTEMPT_LIMIT_PER_HOUR)).toBe(false);
    // Same string, but the "ip" scope has never recorded an attempt for it.
    expect(await isWithinRateLimit(store, "ip", key, IP_ATTEMPT_LIMIT_PER_HOUR)).toBe(true);
  });
});
