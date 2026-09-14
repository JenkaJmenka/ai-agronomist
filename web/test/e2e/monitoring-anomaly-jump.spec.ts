import { expect, test } from "@playwright/test";

/** Anomaly → Monitoring jump: all affected sensors pre-selected, anomalous
 *  chart region banded, anomalous table rows highlighted. All BFF mocked. */

const ANOMALY_HOURS = [46, 47];

function isoAtHour(hour: number): string {
  const base = new Date("2026-06-01T00:00:00Z").getTime();
  return new Date(base + hour * 3_600_000).toISOString().replace(".000Z", "+00:00");
}

function makeRows(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    timestamp: isoAtHour(i),
    // Anomalous hours spike temperature out of the optimal [20, 28] band so
    // only that channel cell is highlighted (not the whole row).
    temperature: ANOMALY_HOURS.includes(i) ? 35 : 21 + (i % 10) * 0.1,
    humidity: 60,
    co2: 400,
    anomaly_score: ANOMALY_HOURS.includes(i) ? 0.9 : 0.01,
  }));
}

function mockRecent(rows: ReturnType<typeof makeRows>) {
  return {
    rows,
    count: rows.length,
    min_timestamp: rows[0]?.timestamp ?? null,
    max_timestamp: rows.at(-1)?.timestamp ?? null,
  };
}

test("jump link carries every affected sensor and the threshold", async ({ page }) => {
  const rows = makeRows(48);
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: mockRecent(rows) }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            timestamp: rows[ANOMALY_HOURS[0]].timestamp,
            anomaly_score: 0.9,
            fault_type: "spike_fault",
            root_cause: "Fan failure",
            affected_sensors: ["temperature", "humidity"],
            incident_id: "inc-1",
          },
        ],
        count: 1,
        anomaly_count: 2,
      },
    }),
  );

  await page.goto("/anomalies");
  // The landing view now opens on merged episodes; these assertions
  // cover the flat per-sample table, which is one toggle away.
  await page.getByTestId("chart-anomaly-mode").getByRole("button", { name: "Samples" }).click();
  const jump = page
    .getByRole("link", {
      name: `Open ${rows[ANOMALY_HOURS[0]].timestamp} in Monitoring`,
    })
    .first();
  await expect(jump).toHaveAttribute("href", /channel=temperature%2Chumidity/);
  await expect(jump).toHaveAttribute("href", /threshold=0.15/);
  await expect(jump).toHaveAttribute("href", /live=false/);
});

test("clicking the jump opens Monitoring with the affected sensors selected", async ({ page }) => {
  const rows = makeRows(48);
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: mockRecent(rows) }),
  );
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            timestamp: rows[ANOMALY_HOURS[0]].timestamp,
            anomaly_score: 0.9,
            fault_type: "spike_fault",
            root_cause: "Fan failure",
            affected_sensors: ["temperature", "humidity"],
            incident_id: "inc-1",
          },
        ],
        count: 1,
        anomaly_count: 2,
      },
    }),
  );

  await page.goto("/anomalies");
  // The landing view now opens on merged episodes; these assertions
  // cover the flat per-sample table, which is one toggle away.
  await page.getByTestId("chart-anomaly-mode").getByRole("button", { name: "Samples" }).click();
  await page
    .getByRole("link", { name: `Open ${rows[ANOMALY_HOURS[0]].timestamp} in Monitoring` })
    .first()
    .click();

  await expect(page).toHaveURL(/\/monitoring\?/);
  const toggles = page.getByTestId("channel-toggles");
  await expect(toggles.getByRole("checkbox", { name: "temperature" })).toBeChecked();
  await expect(toggles.getByRole("checkbox", { name: "humidity" })).toBeChecked();
  await expect(toggles.getByRole("checkbox", { name: "co2" })).not.toBeChecked();
});

test("Gas keeps jump channel= but does not paint anomaly score highlights", async ({ page }) => {
  const rows = makeRows(48);
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );

  await page.goto(
    "/gas?from=2026-06-01T00:00:00.000Z&to=2026-06-03T00:00:00.000Z&threshold=0.15&channel=temperature%2Chumidity&live=false",
  );

  // No gas columns in mock data — still must not emphasize anomaly scores from the jump.
  const flaggedCells = page.locator(
    '[data-testid="data-table"] tbody td[class*="bg-sev-critical"]',
  );
  await expect(flaggedCells).toHaveCount(0);
});

test("monitoring highlights anomalous channel cells and renders the chart", async ({ page }) => {
  const rows = makeRows(48);
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );

  await page.goto(
    "/monitoring?from=2026-06-01T00:00:00.000Z&to=2026-06-03T00:00:00.000Z&threshold=0.15",
  );

  await expect(page.getByTestId("uplot-chart")).toBeVisible();
  // Whole-row highlight is gone — only off-setpoint channel + score cells light up.
  await expect(
    page.locator('[data-testid="data-table"] tbody tr[class*="bg-sev-critical"]'),
  ).toHaveCount(0);
  const flaggedCells = page.locator(
    '[data-testid="data-table"] tbody td[class*="bg-sev-critical"]',
  );
  // 2 anomalous rows × (temperature + score) = 4 highlighted cells.
  await expect(flaggedCells).toHaveCount(ANOMALY_HOURS.length * 2);
  await expect(flaggedCells.first()).toContainText("35.00");
});
