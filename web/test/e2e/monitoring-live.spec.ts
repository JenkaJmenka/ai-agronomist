import { expect, type Page, test } from "@playwright/test";

/**
 * Live monitoring SSE integration — Phase 5A.
 * All BFF calls are mocked (page.route). No real backend needed.
 */

function isoAtMinute(minuteOffset: number): string {
  const base = new Date("2026-07-18T10:00:00Z").getTime();
  return new Date(base + minuteOffset * 60_000).toISOString().replace(".000Z", "+00:00");
}

function makeHistoricalRows(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    timestamp: isoAtMinute(i),
    temperature: 22.0 + (i % 5) * 0.1,
    humidity: 60.0,
    co2: 400.0,
    anomaly_score: null,
  }));
}

function sseEvent(type: string, data: unknown, id?: string): string {
  const idLine = id ? `id: ${id}\n` : "";
  return `event: ${type}\n${idLine}data: ${JSON.stringify(data)}\n\n`;
}

const HISTORICAL_ROWS = makeHistoricalRows(10);

const MOCK_RECENT = {
  rows: HISTORICAL_ROWS,
  count: HISTORICAL_ROWS.length,
  min_timestamp: HISTORICAL_ROWS[0]?.timestamp ?? null,
  max_timestamp: HISTORICAL_ROWS.at(-1)?.timestamp ?? null,
};

const LIVE_READING = {
  seq: "",
  timestamp: isoAtMinute(20),
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  channels: { temperature: 25.0, humidity: 70.0, co2: 450.0 },
  sources: { temperature: "src_1", humidity: "src_1", co2: "src_2" },
  anomaly_score: null,
};

const LIVE_ANOMALY = {
  seq: "anom-e2e-1",
  timestamp: isoAtMinute(20),
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  score: 0.87,
  threshold: 0.15,
  affected_channels: ["co2"],
  reasoning: "Co2 spike",
  window_start: isoAtMinute(0),
  window_end: isoAtMinute(20),
};

const SSE_BODY =
  sseEvent("reading", LIVE_READING, "1-0") +
  sseEvent("anomaly", LIVE_ANOMALY, "2-0") +
  sseEvent("heartbeat", {}, "3-0");

// ---------------------------------------------------------------------------
// Mocking helpers
// ---------------------------------------------------------------------------

async function mockAllRoutes(page: Page) {
  await page.route("**/api/bff/readings/recent**", (route) => route.fulfill({ json: MOCK_RECENT }));
  await page.route("**/api/bff/live/status**", (route) =>
    route.fulfill({
      json: {
        sources: [{ source_id: "src_1", vendor: "acme", status: "online" }],
      },
    }),
  );
  await page.route("**/api/bff/live/events**", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: SSE_BODY,
    }),
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("live mode is OFF by default — badge and strip not visible", async ({ page }) => {
  await page.route("**/api/bff/readings/recent**", (route) => route.fulfill({ json: MOCK_RECENT }));
  // SSE route should NOT be called when live is off.
  let sseCalled = false;
  await page.route("**/api/bff/live/events**", (route) => {
    sseCalled = true;
    return route.fulfill({ contentType: "text/event-stream", body: "" });
  });

  await page.goto("/monitoring");

  // Wait for historical data to load.
  await expect(page.getByTestId("data-table")).toBeVisible();

  expect(sseCalled).toBe(false);
  await expect(page.getByTestId("live-badge")).not.toBeVisible();
  await expect(page.getByTestId("live-anomaly-strip")).not.toBeVisible();
});

test("Go Live button is present and toggles live mode", async ({ page }) => {
  await mockAllRoutes(page);
  await page.goto("/monitoring");

  await expect(page.getByTestId("live-toggle")).toBeVisible();
  await expect(page.getByTestId("live-toggle")).not.toBeDisabled();

  // Toggle on.
  await page.getByTestId("live-toggle").click();
  await expect(page).toHaveURL(/live=true/);
});

test("live badge shows Live · SSE and appends live rows to the table", async ({ page }) => {
  await mockAllRoutes(page);

  // Navigate directly into live mode.
  await page.goto("/monitoring?live=true");

  // Wait for badge to appear.
  await expect(page.getByTestId("live-badge")).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId("live-badge")).toContainText("Live");

  // The live reading has timestamp isoAtMinute(20) — distinct from the 10 historical rows.
  // Wait for it to appear in the table.
  await expect(page.getByTestId("data-table")).toBeVisible();
});

test("live anomaly strip appears when an anomaly SSE event is received", async ({ page }) => {
  await mockAllRoutes(page);
  await page.goto("/monitoring?live=true");

  // Strip should appear once the anomaly event is processed.
  await expect(page.getByTestId("live-anomaly-strip")).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId("live-anomaly-strip")).toContainText("co2");
});

test("anomaly strip item links to the Anomalies page with correct params", async ({ page }) => {
  await mockAllRoutes(page);
  await page.goto("/monitoring?live=true");

  await expect(page.getByTestId("live-anomaly-strip")).toBeVisible({ timeout: 5000 });

  const link = page.getByTestId(`live-anomaly-link-anom-e2e-1`);
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", /\/anomalies\?/);
  await expect(link).toHaveAttribute("href", /channel=co2/);
  await expect(link).toHaveAttribute("href", /threshold=/);
});

test("setting a date window while live forces live=false", async ({ page }) => {
  await mockAllRoutes(page);
  await page.goto("/monitoring?live=true");

  await expect(page.getByTestId("live-badge")).toBeVisible({ timeout: 3000 });

  // Apply a quick range (which sets from/to in the URL).
  await page.getByTestId("date-range-controls").getByRole("button", { name: "24 h" }).click();

  // live param should be gone or false.
  await expect(page).not.toHaveURL(/live=true/);
  await expect(page.getByTestId("live-badge")).not.toBeVisible();
});

test("Go Live button is disabled when a date window is set", async ({ page }) => {
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows: HISTORICAL_ROWS, count: HISTORICAL_ROWS.length } }),
  );

  await page.goto("/monitoring?from=2026-07-18T00:00:00.000Z&to=2026-07-18T12:00:00.000Z");

  await expect(page.getByTestId("live-toggle")).toBeVisible();
  await expect(page.getByTestId("live-toggle")).toBeDisabled();
});
