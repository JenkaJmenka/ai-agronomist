import { expect, test } from "@playwright/test";

/**
 * Performance budgets from MIGRATION_PLAN.md §1.2, asserted in CI — the
 * suite FAILS when a budget is breached. Data is mocked at the BFF at the
 * downsampling contract's ceiling (2k rows) so numbers reflect render
 * cost, not backend latency.
 */

const COLD_TTI_BUDGET_MS = 2000;
const FILTER_APPLY_BUDGET_MS = 500;
const CHANNEL_TOGGLE_BUDGET_MS = 300;
const DOWNSAMPLED_ROWS = 2000;

function isoAtMinute(minute: number): string {
  const base = new Date("2026-06-01T00:00:00Z").getTime();
  return new Date(base + minute * 60_000).toISOString().replace(".000Z", "+00:00");
}

const ROWS = Array.from({ length: DOWNSAMPLED_ROWS }, (_, i) => ({
  timestamp: isoAtMinute(i),
  temperature: 21 + Math.sin(i / 60) * 2,
  humidity: 60 + Math.cos(i / 90) * 5,
  co2: 400 + (i % 50),
  light: 500 + (i % 100),
  anomaly_score: i % 200 === 0 ? 0.9 : null,
}));

test.beforeEach(async ({ page }) => {
  // Profiling must not contend with perf budgets (enabled probe / POST).
  await page.route("**/api/profile**", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ json: { enabled: false } });
    }
    return route.fulfill({ status: 204, body: "" });
  });
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({
      json: {
        rows: ROWS,
        count: ROWS.length,
        min_timestamp: ROWS[0].timestamp,
        max_timestamp: ROWS.at(-1)?.timestamp,
        downsampled: true,
        original_count: 50_000,
      },
    }),
  );
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows: ROWS, count: ROWS.length, downsampled: true } }),
  );
  await page.route("**/api/bff/anomalies/**", (route) =>
    route.fulfill({ json: { rows: [], count: 0, anomaly_count: 0, groups: [] } }),
  );
});

test(`anomalies cold load paints content under ${COLD_TTI_BUDGET_MS} ms`, async ({ page }) => {
  const startedAt = Date.now();
  await page.goto("/anomalies");
  await expect(page.getByRole("heading", { name: "Anomaly Detection" })).toBeVisible();
  await expect(page.getByText(/anomalies ≥/)).toBeVisible();
  const elapsed = Date.now() - startedAt;
  expect(elapsed, `cold TTI ${elapsed} ms`).toBeLessThan(COLD_TTI_BUDGET_MS);
});

test(`filter apply updates the view under ${FILTER_APPLY_BUDGET_MS} ms`, async ({ page }) => {
  await page.goto("/anomalies");
  await expect(page.getByText(/anomalies ≥/)).toBeVisible();

  const startedAt = Date.now();
  await page.getByTestId("period-select").selectOption("7d");
  // URL-driven window applied and the (mock-instant) refetch rendered.
  await expect(page).toHaveURL(/from=/);
  const elapsed = Date.now() - startedAt;
  expect(elapsed, `filter apply ${elapsed} ms`).toBeLessThan(FILTER_APPLY_BUDGET_MS);
});

test(`channel toggle re-renders client-side under ${CHANNEL_TOGGLE_BUDGET_MS} ms`, async ({
  page,
}) => {
  await page.goto("/monitoring");
  const toggles = page.getByTestId("channel-toggles");
  const temperature = toggles.getByRole("checkbox", { name: "temperature" });
  const humidity = toggles.getByRole("checkbox", { name: "humidity" });
  await expect(temperature).toBeChecked();
  await expect(humidity).toBeChecked();

  // Warm the chart setData path so the budget measures steady-state toggle cost,
  // not first-interaction JIT / layout (CI runners were flaking at 309 vs 300).
  await humidity.uncheck();
  await expect(humidity).not.toBeChecked();
  await humidity.check();
  await expect(humidity).toBeChecked();

  let readingsRefetches = 0;
  await page.route("**/api/bff/readings/**", (route) => {
    readingsRefetches += 1;
    return route.continue();
  });

  const startedAt = Date.now();
  await temperature.uncheck();
  await expect(temperature).not.toBeChecked();
  const elapsed = Date.now() - startedAt;
  expect(elapsed, `channel toggle ${elapsed} ms`).toBeLessThan(CHANNEL_TOGGLE_BUDGET_MS);
  // The §1.2 contract: toggling never refetches — pure client state.
  expect(readingsRefetches).toBe(0);
  // Table columns follow ChannelToggles: hidden sensor drops its column.
  const table = page.getByTestId("data-table");
  await expect(table.getByRole("columnheader", { name: "temperature" })).toHaveCount(0);
  await expect(table.getByRole("columnheader", { name: "humidity" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Timestamp" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Score" })).toBeVisible();
});
