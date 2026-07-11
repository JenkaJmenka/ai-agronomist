import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./test/e2e",
  timeout: 30_000,
  use: {
    baseURL: "http://localhost:3100",
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
    },
  },
});
