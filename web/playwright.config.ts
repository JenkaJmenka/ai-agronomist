import { defineConfig } from "@playwright/test";

import { E2E_AUTH_SECRET } from "./test/e2e/authSecret";

export default defineConfig({
  testDir: "./test/e2e",
  timeout: 30_000,
  globalSetup: "./test/e2e/global-setup.ts",
  use: {
    baseURL: "http://localhost:3100",
    // Pre-authenticated session (security phase 2, §2) — every page now
    // sits behind login; see global-setup.ts for how this is minted.
    storageState: "./test/e2e/.auth/storageState.json",
  },
  webServer: {
    command: "pnpm start --port 3100",
    url: "http://localhost:3100",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: {
      // E2E must be hermetic: every BFF call is mocked with page.route, so
      // point the proxy at a dead port. A locally running API can otherwise
      // mask missing mocks that then time out in CI (which has no backend).
      API_URL: process.env.E2E_API_URL ?? "http://127.0.0.1:9999",
      // Must match global-setup.ts's signing secret exactly.
      AUTH_SECRET: E2E_AUTH_SECRET,
    },
  },
});
