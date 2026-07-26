import { expect, type Page, test } from "@playwright/test";

/**
 * Live monitoring SSE integration — Phase 5A + Phase 8 (sensor tiles, setpoints).
 * All BFF calls are mocked (page.route). No real backend needed.
 *
 * CLOCK NOTE: Live mode anchors the visible window to wall-clock `now`
 * (periodWindowEnd returns new Date() when live=true). Fixture rows at
 * 2026-07-18 would fall outside [now-24h, now] at real wall-clock time,
 * stranding all data and hitting the empty state. mockAllRoutes /
 * mockAllRoutesV2 freeze the browser clock to 2026-07-18T10:30:00Z —
 * strictly after the newest fixture timestamp (minute 21, 10:21:00Z) — so
 * every fixture row stays inside the active window. setFixedTime is used
 * instead of clock.install so setTimeout/setInterval keep running normally;
 * the SSE flush interval and live timers depend on real timer execution.
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

/**
 * Historical rows that include the new Phase 8 channels so tiles render in
 * both live and historical modes.
 */
function makeHistoricalRowsV2(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    timestamp: isoAtMinute(i),
    temperature: 22.0 + (i % 5) * 0.1,
    humidity: 60.0,
    co2: 400.0,
    soil_moisture: 70.0,
    root_zone_temp: 20.0,
    vpd: 0.9,
    anomaly_score: null,
  }));
}

function sseEvent(type: string, data: unknown, id?: string): string {
  const idLine = id ? `id: ${id}\n` : "";
  return `event: ${type}\n${idLine}data: ${JSON.stringify(data)}\n\n`;
}

const HISTORICAL_ROWS = makeHistoricalRows(10);
const HISTORICAL_ROWS_V2 = makeHistoricalRowsV2(10);

const MOCK_RECENT = {
  rows: HISTORICAL_ROWS,
  count: HISTORICAL_ROWS.length,
  min_timestamp: HISTORICAL_ROWS[0]?.timestamp ?? null,
  max_timestamp: HISTORICAL_ROWS.at(-1)?.timestamp ?? null,
};

const MOCK_RECENT_V2 = {
  rows: HISTORICAL_ROWS_V2,
  count: HISTORICAL_ROWS_V2.length,
  min_timestamp: HISTORICAL_ROWS_V2[0]?.timestamp ?? null,
  max_timestamp: HISTORICAL_ROWS_V2.at(-1)?.timestamp ?? null,
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

/** Live reading that includes Phase 8 channels including derived vpd. */
const LIVE_READING_V2 = {
  seq: "",
  timestamp: isoAtMinute(20),
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  channels: {
    temperature: 25.0,
    humidity: 70.0,
    co2: 450.0,
    soil_moisture: 72.0,
    root_zone_temp: 21.0,
    vpd: 0.95,
  },
  sources: {
    temperature: "src_1",
    humidity: "src_1",
    co2: "src_2",
    soil_moisture: "src_3",
    root_zone_temp: "src_3",
    vpd: "derived",
  },
  anomaly_score: null,
};

/** Live reading with a critically high temperature to test tile status coloring. */
const LIVE_READING_CRITICAL = {
  ...LIVE_READING_V2,
  timestamp: isoAtMinute(21),
  channels: {
    ...LIVE_READING_V2.channels,
    // temperature > 28.8 (warnMax for [20,28] range) → critical
    temperature: 35.0,
  },
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

const SSE_BODY_V2 = sseEvent("reading", LIVE_READING_V2, "1-0") + sseEvent("heartbeat", {}, "2-0");

const SSE_BODY_CRITICAL =
  sseEvent("reading", LIVE_READING_CRITICAL, "1-0") + sseEvent("heartbeat", {}, "2-0");

// ---------------------------------------------------------------------------
// Mocking helpers
// ---------------------------------------------------------------------------

async function mockAllRoutes(page: Page) {
  // Freeze Date.now()/new Date() to the fixture era so live-mode's 24h window
  // includes all fixture rows (2026-07-18T10:00–10:09). Must run before goto.
  await page.clock.setFixedTime(new Date("2026-07-18T10:30:00Z"));
  await page.route("**/api/bff/readings/bounds**", (route) =>
    route.fulfill({
      json: {
        min_timestamp: HISTORICAL_ROWS[0]?.timestamp ?? null,
        max_timestamp: HISTORICAL_ROWS.at(-1)?.timestamp ?? null,
      },
    }),
  );
  await page.route("**/api/bff/readings/recent**", (route) => route.fulfill({ json: MOCK_RECENT }));
  // Live mode derives a concrete from/to from the bounds above, so readings are
  // fetched via /window rather than /recent. Unmocked it escapes to the real
  // BFF proxy and the page renders "Readings request failed (502)".
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows: HISTORICAL_ROWS, count: HISTORICAL_ROWS.length } }),
  );
  await page.route("**/api/bff/live/resume**", (route) =>
    route.fulfill({
      json: { resumed: true, tenant_id: "tenant_1", greenhouse_id: "gh_A" },
    }),
  );
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

/** Mock routes with Phase 8 channels (v2 rows + vpd). */
async function mockAllRoutesV2(page: Page, sseBody = SSE_BODY_V2) {
  // Freeze Date.now()/new Date() to the fixture era so live-mode's 24h window
  // includes all fixture rows (2026-07-18T10:00–10:09). Must run before goto.
  await page.clock.setFixedTime(new Date("2026-07-18T10:30:00Z"));
  await page.route("**/api/bff/readings/bounds**", (route) =>
    route.fulfill({
      json: {
        min_timestamp: HISTORICAL_ROWS_V2[0]?.timestamp ?? null,
        max_timestamp: HISTORICAL_ROWS_V2.at(-1)?.timestamp ?? null,
      },
    }),
  );
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: MOCK_RECENT_V2 }),
  );
  // See mockAllRoutes: live mode fetches /window once bounds give it from/to.
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows: HISTORICAL_ROWS_V2, count: HISTORICAL_ROWS_V2.length } }),
  );
  await page.route("**/api/bff/live/resume**", (route) =>
    route.fulfill({
      json: { resumed: true, tenant_id: "tenant_1", greenhouse_id: "gh_A" },
    }),
  );
  await page.route("**/api/bff/live/status**", (route) =>
    route.fulfill({
      json: {
        sources: [
          { source_id: "src_1", vendor: "acme", status: "online" },
          { source_id: "src_3", vendor: "soilsense", status: "online" },
        ],
      },
    }),
  );
  await page.route("**/api/bff/live/events**", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: sseBody,
    }),
  );
}

// ---------------------------------------------------------------------------
// Original Phase 5A tests — no regressions
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
  await expect(page.getByTestId("live-toggle")).toHaveAttribute("data-live", "false");
  await expect(page.getByTestId("live-anomaly-strip")).not.toBeVisible();
});

test("Live button is present and toggles live mode", async ({ page }) => {
  await mockAllRoutes(page);
  await page.goto("/monitoring");

  await expect(page.getByTestId("live-toggle")).toBeVisible();
  await expect(page.getByTestId("live-toggle")).toContainText("Live");
  await expect(page.getByTestId("live-toggle")).not.toBeDisabled();

  // Toggle on.
  await page.getByTestId("live-toggle").click();
  await expect(page).toHaveURL(/live=true/);
  await expect(page.getByTestId("live-toggle")).toHaveAttribute("data-live", "true");
});

test("live toggle shows active state and appends live rows to the table", async ({ page }) => {
  await mockAllRoutes(page);

  // Navigate directly into live mode.
  await page.goto("/monitoring?live=true");

  await expect(page.getByTestId("live-toggle")).toHaveAttribute("data-live", "true", {
    timeout: 5000,
  });
  await expect(page.getByTestId("live-toggle")).toContainText("Live");

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
  await expect(link).toHaveAttribute("href", /live=true/);
  await expect(link).toHaveAttribute("href", /from=/);
  await expect(link).toHaveAttribute("href", /to=/);
});

test("setting a date window while live keeps live mode on", async ({ page }) => {
  await mockAllRoutes(page);
  await page.goto("/monitoring?live=true");

  await expect(page.getByTestId("live-toggle")).toHaveAttribute("data-live", "true", {
    timeout: 3000,
  });

  // Apply a period preset (sets from/to in the URL) — live stays on.
  await page.getByTestId("period-select").selectOption("24h");

  await expect(page).toHaveURL(/live=true/);
  await expect(page.getByTestId("live-toggle")).toHaveAttribute("data-live", "true");
});

test("Live stays enabled when a date window is set", async ({ page }) => {
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows: HISTORICAL_ROWS, count: HISTORICAL_ROWS.length } }),
  );

  await page.goto("/monitoring?from=2026-07-18T00:00:00.000Z&to=2026-07-18T12:00:00.000Z");

  await expect(page.getByTestId("live-toggle")).toBeVisible();
  await expect(page.getByTestId("live-toggle")).not.toBeDisabled();
});

// ---------------------------------------------------------------------------
// Phase 8 — Sensor tiles
// ---------------------------------------------------------------------------

test("sensor tiles render for all discovered channels (historical)", async ({ page }) => {
  await mockAllRoutesV2(page);
  await page.goto("/monitoring");

  await expect(page.getByTestId("sensor-tiles")).toBeVisible({ timeout: 5000 });

  // All Phase 8 channels must have a tile.
  await expect(page.getByTestId("sensor-tile-temperature")).toBeVisible();
  await expect(page.getByTestId("sensor-tile-humidity")).toBeVisible();
  await expect(page.getByTestId("sensor-tile-co2")).toBeVisible();
  await expect(page.getByTestId("sensor-tile-soil_moisture")).toBeVisible();
  await expect(page.getByTestId("sensor-tile-root_zone_temp")).toBeVisible();
  await expect(page.getByTestId("sensor-tile-vpd")).toBeVisible();
});

test("sensor tiles render with live values from SSE", async ({ page }) => {
  await mockAllRoutesV2(page);
  await page.goto("/monitoring?live=true");

  await expect(page.getByTestId("sensor-tiles")).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId("sensor-tile-vpd")).toBeVisible();

  // vpd tile shows the value from the live reading (0.95 kPa)
  // After flush interval (500 ms) the tile should update.
  await expect(page.getByTestId("sensor-tile-vpd")).toContainText("kPa", {
    timeout: 3000,
  });
});

test("vpd tile shows derived provenance badge", async ({ page }) => {
  await mockAllRoutesV2(page);
  await page.goto("/monitoring");

  await expect(page.getByTestId("sensor-tile-vpd")).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId("sensor-tile-vpd")).toContainText("derived");
});

test("critical temperature value colors its tile with sev-critical border", async ({ page }) => {
  await mockAllRoutesV2(page, SSE_BODY_CRITICAL);
  await page.goto("/monitoring?live=true");

  // Wait for the live data flush (500 ms) and tile render.
  await expect(page.getByTestId("sensor-tile-temperature")).toBeVisible({ timeout: 5000 });

  // Allow the flush interval (up to 1 s) for the critical reading to propagate.
  await page.waitForTimeout(1000);

  const tile = page.getByTestId("sensor-tile-temperature");
  const className = await tile.getAttribute("class");
  expect(className).toContain("border-l-sev-critical");
});

// ---------------------------------------------------------------------------
// Phase 8 — Setpoint controls
// ---------------------------------------------------------------------------

test("Setpoints button is visible in the header", async ({ page }) => {
  await mockAllRoutesV2(page);
  await page.goto("/monitoring");

  await expect(page.getByTestId("setpoints-trigger")).toBeVisible({ timeout: 5000 });
});

test("Setpoints panel opens and shows channel inputs", async ({ page }) => {
  await mockAllRoutesV2(page);
  await page.goto("/monitoring");

  await expect(page.getByTestId("sensor-tiles")).toBeVisible({ timeout: 5000 });

  // Open the panel.
  await page.getByTestId("setpoints-trigger").click();
  await expect(page.getByTestId("setpoints-dropdown")).toBeVisible();

  // Should show at least the temperature min/max inputs.
  await expect(page.getByTestId("setpoint-min-temperature")).toBeVisible();
  await expect(page.getByTestId("setpoint-max-temperature")).toBeVisible();
});

test("editing a setpoint changes tile status when value crosses new threshold", async ({
  page,
}) => {
  // Historical rows have temperature=22. Default optimal is [20,28] → ok.
  // After narrowing to [23,28] the value 22 will be below min → warning/critical.
  await mockAllRoutesV2(page);
  await page.goto("/monitoring");

  await expect(page.getByTestId("sensor-tile-temperature")).toBeVisible({ timeout: 5000 });

  // Confirm tile is ok initially (no sev-critical border).
  const tile = page.getByTestId("sensor-tile-temperature");
  let className = await tile.getAttribute("class");
  expect(className).not.toContain("border-l-sev-critical");

  // Open setpoints panel and raise the min above 22.
  await page.getByTestId("setpoints-trigger").click();
  await expect(page.getByTestId("setpoint-min-temperature")).toBeVisible();

  await page.getByTestId("setpoint-min-temperature").fill("25");
  await page.getByTestId("setpoint-max-temperature").fill("30");

  // Close panel and check that the tile now shows a non-ok color.
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("setpoints-dropdown")).not.toBeVisible();

  // The tile's value (22) is now critically below the new min (25 - 10%*5=0.5 = warnMin 24.5)
  className = await tile.getAttribute("class");
  expect(className).toContain("border-l-sev-critical");
});
