import { expect, type Page, test } from "@playwright/test";

/**
 * Sensor tiles carry two INDEPENDENT signals:
 *   - setpoint state (border colour): latest value vs the configured range;
 *   - detector state (anomaly badge): anomalies whose affected_sensors name
 *     the channel in the current window.
 *
 * Regression for 2026-10-26: a heating-temperature anomaly showed a GREEN
 * heating tile (no range defined → silently "ok") while humidity was RED with
 * no humidity anomaly (it was only above its setpoint). All BFF mocked.
 */

const WINDOW =
  "from=2026-06-01T00:00:00.000Z&to=2026-06-03T00:00:00.000Z&threshold=0.15&live=false";
const ANOMALY_HOURS = [40, 41, 42];

function isoAtHour(hour: number): string {
  const base = new Date("2026-06-01T00:00:00Z").getTime();
  return new Date(base + hour * 3_600_000).toISOString().replace(".000Z", "+00:00");
}

function makeRows(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    timestamp: isoAtHour(i),
    temperature: 24,
    // Well above the [53, 77] humidity range → critical setpoint state.
    humidity: 92,
    // No CHANNEL_META entry and no user setpoint → no range.
    heating_temp: ANOMALY_HOURS.includes(i) ? 18 : 45,
    co2: 400,
    gas_co2: 800,
    anomaly_score: ANOMALY_HOURS.includes(i) ? 0.9 : 0.01,
  }));
}

async function mockScenario(page: Page, affectedSensors: readonly string[]) {
  const rows = makeRows(48);
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({
      json: {
        rows: ANOMALY_HOURS.map((h) => ({
          timestamp: rows[h].timestamp,
          anomaly_score: 0.9,
          fault_type: "spike_fault",
          affected_sensors: affectedSensors,
        })),
        count: ANOMALY_HOURS.length,
        anomaly_count: ANOMALY_HOURS.length,
      },
    }),
  );
}

test("heating anomaly badges the heating tile, not the out-of-range humidity tile", async ({
  page,
}) => {
  await mockScenario(page, ["heating_temp"]);
  await page.goto(`/monitoring?${WINDOW}`);

  const heating = page.getByTestId("sensor-tile-heating_temp");
  const humidity = page.getByTestId("sensor-tile-humidity");
  const temperature = page.getByTestId("sensor-tile-temperature");
  await expect(heating).toBeVisible({ timeout: 5000 });

  // Detector signal: only the attributed channel carries the badge.
  await expect(page.getByTestId("sensor-tile-anomaly-heating_temp")).toHaveText("3 anomalies");
  await expect(page.getByTestId("sensor-tile-anomaly-humidity")).toHaveCount(0);
  await expect(page.getByTestId("sensor-tile-anomaly-temperature")).toHaveCount(0);

  // Setpoint signal: unchanged by the detector, and never green without a range.
  await expect(heating).toHaveAttribute("data-setpoint-state", "no_range");
  await expect(heating).not.toHaveClass(/border-l-sev-normal/);
  await expect(humidity).toHaveAttribute("data-setpoint-state", "critical");
  // Humidity is out of setpoint range but has NO detector anomaly → amber, not red.
  await expect(humidity).toHaveClass(/border-l-sev-warning/);
  await expect(humidity).not.toHaveClass(/border-l-sev-critical/);
  await expect(temperature).toHaveAttribute("data-setpoint-state", "ok");
  await expect(temperature).toHaveClass(/border-l-sev-normal/);
});

test("no tile shows an anomaly badge when the detector flagged nothing", async ({ page }) => {
  const rows = makeRows(48);
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({ json: { rows: [], count: 0, anomaly_count: 0 } }),
  );
  await page.goto(`/monitoring?${WINDOW}`);

  await expect(page.getByTestId("sensor-tile-humidity")).toBeVisible({ timeout: 5000 });
  // Humidity is out of range, which is a setpoint excursion, not an anomaly.
  await expect(page.getByTestId("sensor-tile-humidity")).toHaveAttribute(
    "data-setpoint-state",
    "critical",
  );
  await expect(page.locator('[data-testid^="sensor-tile-anomaly-"]')).toHaveCount(0);
});

test("a climate co2 anomaly does not badge gas_co2 on the Gas page", async ({ page }) => {
  await mockScenario(page, ["co2"]);
  await page.goto(`/gas?${WINDOW}`);

  await expect(page.getByTestId("sensor-tile-gas_co2")).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId("sensor-tile-anomaly-gas_co2")).toHaveCount(0);
});

/**
 * Reported case 2026-10-26: measured_hd anomaly + humidity 29% (below setpoint 53–77).
 * measured_hd → red border + badge (anomaly is the dominant visual signal).
 * humidity → amber border (setpoint excursion, NOT anomaly), no badge, shows related hint.
 */
test("measured_hd anomaly: measured_hd tile is red with badge; humidity is amber with related hint, no badge", async ({
  page,
}) => {
  const rows = Array.from({ length: 48 }, (_, i) => ({
    timestamp: isoAtHour(i),
    temperature: 24,
    // Well below the [53, 77] humidity range → critical setpoint state, no anomaly.
    humidity: 29,
    // measured_hd has no configured setpoint range but has detector anomalies.
    measured_hd: ANOMALY_HOURS.includes(i) ? 14.2 : 11.0,
    co2: 400,
    anomaly_score: ANOMALY_HOURS.includes(i) ? 0.9 : 0.01,
  }));

  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({
      json: {
        rows: ANOMALY_HOURS.map((h) => ({
          timestamp: rows[h].timestamp,
          anomaly_score: 0.9,
          fault_type: "spike_fault",
          affected_sensors: ["measured_hd"],
        })),
        count: ANOMALY_HOURS.length,
        anomaly_count: ANOMALY_HOURS.length,
      },
    }),
  );

  await page.goto(`/monitoring?${WINDOW}`);

  const measuredHd = page.getByTestId("sensor-tile-measured_hd");
  const humidity = page.getByTestId("sensor-tile-humidity");
  await expect(measuredHd).toBeVisible({ timeout: 5000 });

  // measured_hd: detector anomaly present → RED border (anomaly dominant) + badge.
  await expect(page.getByTestId("sensor-tile-anomaly-measured_hd")).toHaveText("3 anomalies");
  await expect(measuredHd).toHaveClass(/border-l-sev-critical/);

  // humidity: setpoint excursion only (humidity 29 is below [53, 77]) → AMBER, not red.
  await expect(page.getByTestId("sensor-tile-anomaly-humidity")).toHaveCount(0);
  await expect(humidity).toHaveAttribute("data-setpoint-state", "critical");
  await expect(humidity).toHaveClass(/border-l-sev-warning/);
  await expect(humidity).not.toHaveClass(/border-l-sev-critical/);

  // humidity tile shows the directional setpoint hint.
  await expect(page.getByTestId("sensor-tile-setpoint-hint-humidity")).toBeVisible();

  // humidity tile shows the "related to measured HD anomaly" hint (RELATED_CHANNELS coupling).
  await expect(page.getByTestId("sensor-tile-related-humidity")).toBeVisible();
  await expect(humidity).toHaveAttribute("data-related-anomaly", "measured_hd");
});
