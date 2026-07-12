import { expect, test } from "@playwright/test";

/** P2/P1 deferred items: fault labels (G4), server groups (G3),
 *  channel-aware Monitoring jump, virtualized tables. All BFF mocked. */

function isoAtHour(hour: number): string {
  const base = new Date("2026-06-01T00:00:00Z").getTime();
  return new Date(base + hour * 3_600_000).toISOString().replace(".000Z", "+00:00");
}

function makeRows(count: number, scoredEvery: number) {
  return Array.from({ length: count }, (_, i) => ({
    timestamp: isoAtHour(i),
    temperature: 21 + (i % 10) * 0.1,
    humidity: 60,
    anomaly_score: i % scoredEvery === 0 ? 0.9 : null,
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

test("top anomalies show G4 fault labels and channel-aware jump", async ({ page }) => {
  const rows = makeRows(48, 24); // 2 anomalies
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: mockRecent(rows) }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            timestamp: rows[0].timestamp,
            anomaly_score: 0.9,
            fault_type: "spike_fault",
            root_cause: "Fan failure",
            affected_sensors: ["temperature"],
            incident_id: "inc-1",
          },
        ],
        count: 1,
        anomaly_count: 2,
      },
    }),
  );

  await page.goto("/anomalies");
  await expect(page.getByTestId("fault-type").first()).toHaveText("Spike fault");

  const jump = page.getByRole("link", { name: `Open ${rows[0].timestamp} in Monitoring` });
  await expect(jump).toHaveAttribute("href", /channel=temperature/);
});

test("monitoring channel deep link pre-selects the toggle", async ({ page }) => {
  const rows = makeRows(48, 100);
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );

  await page.goto(
    "/monitoring?from=2026-06-01T00:00:00.000Z&to=2026-06-03T00:00:00.000Z&channel=temperature",
  );
  const toggles = page.getByTestId("channel-toggles");
  await expect(toggles.getByRole("checkbox", { name: "temperature" })).toBeChecked();
  await expect(toggles.getByRole("checkbox", { name: "humidity" })).not.toBeChecked();
});

test("grouped view switches to G3 server groups at 500+ anomalies", async ({ page }) => {
  // Every row scored → 600 anomalies, above SERVER_GROUPING_THRESHOLD.
  const rows = makeRows(600, 1);
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: mockRecent(rows) }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({ json: { rows: [], count: 0, anomaly_count: 600 } }),
  );
  let requestedGroupBy: string | null = null;
  await page.route("**/api/bff/anomalies/groups**", (route) => {
    requestedGroupBy = new URL(route.request().url()).searchParams.get("group_by");
    return route.fulfill({
      json: {
        groups: [
          {
            key: requestedGroupBy === "type" ? "spike_fault" : "2026-06-01",
            count: 600,
            top_score: 0.9,
            sample_ids: [rows[0].timestamp],
          },
        ],
        group_by: requestedGroupBy,
        anomaly_count: 600,
      },
    });
  });

  await page.goto("/anomalies");
  await page.getByRole("tab", { name: "Table view" }).click();

  const serverGrouped = page.getByTestId("server-grouped");
  await expect(serverGrouped).toBeVisible();
  await expect(serverGrouped.getByText(/grouped server-side/)).toBeVisible();
  await expect(serverGrouped.getByText(/600 record/)).toBeVisible();

  await serverGrouped.getByRole("button", { name: "Fault type" }).click();
  await expect(serverGrouped.getByText("Spike fault")).toBeVisible();
  expect(requestedGroupBy).toBe("type");
});

test("grouped view counts exact G2 flagged rows, not downsampled chart rows", async ({ page }) => {
  // Chart payload carries only 2 scored rows (downsampling artifact) …
  const rows = makeRows(48, 24);
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: mockRecent(rows) }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({ json: { rows: [], count: 0, anomaly_count: 5 } }),
  );
  // … but the window truly holds 5 flagged rows across 3 dates (G2).
  const flaggedRows = [0, 1, 25, 26, 50].map((hour) => ({
    timestamp: isoAtHour(hour),
    temperature: 30,
    anomaly_score: 0.9,
  }));
  await page.route("**/api/bff/readings/anomalies-in-window**", (route) =>
    route.fulfill({ json: { rows: flaggedRows, anomaly_count: 5, total_count: 48 } }),
  );

  await page.goto("/anomalies");
  await page.getByRole("tab", { name: "Table view" }).click();
  await expect(page.getByText("5 anomalies · 3 groups")).toBeVisible();
});

test("large readings table renders virtualized without a row cap", async ({ page }) => {
  const rows = makeRows(600, 100);
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: mockRecent(rows) }),
  );

  await page.goto("/monitoring");
  await expect(page.getByText("Readings (600 rows, newest first)")).toBeVisible();
  await expect(page.getByTestId("data-table")).toHaveAttribute("data-virtualized", "true");
});
