import { chromium } from "@playwright/test";
import { encode } from "next-auth/jwt";

import { E2E_AUTH_SECRET } from "./authSecret";

/**
 * Mints a valid Auth.js session cookie once per e2e run and saves it as
 * Playwright `storageState` (security phase 2, §2) — every spec loads pages
 * that now sit behind login, and none of them exercise the magic-link form
 * itself, so signing a token directly is far cheaper than driving real UI.
 *
 * Cookie name/salt (`authjs.session-token`) matches Auth.js v5's default
 * unprefixed session cookie — the "unprefixed" part depends on the request
 * being plain http (localhost here), which it is.
 */
const SESSION_COOKIE_NAME = "authjs.session-token";
const STORAGE_STATE_PATH = "./test/e2e/.auth/storageState.json";

export default async function globalSetup(): Promise<void> {
  const sessionToken = await encode({
    secret: E2E_AUTH_SECRET,
    salt: SESSION_COOKIE_NAME,
    token: {
      sub: "e2e-test-user",
      email: "e2e@ai-agronomist.test",
      tenantId: "tenant_1",
    },
  });

  const browser = await chromium.launch();
  const context = await browser.newContext();
  await context.addCookies([
    {
      name: SESSION_COOKIE_NAME,
      value: sessionToken,
      domain: "localhost",
      path: "/",
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);
  await context.storageState({ path: STORAGE_STATE_PATH });
  await browser.close();
}
