import { expect, test } from "@playwright/test";

/**
 * Part 2 e2e tests: setpoint context on the Anomaly Detection page.
 *
 * 2A — setpoint context line on every detector anomaly (episode summary + samples).
 * 2B — "Out of setpoint range — not flagged by the detector" excursions section.
 *
 * All BFF routes mocked.
 */

const WINDOW =
  "from=2026-06-01T00:00:00.000Z&to=2026-06-03T00:00:00.000Z&threshold=0.15&live=false";

function isoAtHour(hour: number): string {
  const base = new Date("2026-06-01T00:00:00Z").getTime();
  return new Date(base + hour * 3_600_000).toISOString().replace(".000Z", "+00:00");
}

/** Readings rows: temperature 19.3°C (below [20, 28] setpoint) for hours 0-5,
 *  normal afterwards. Humidity stays normal at 60 (within [53, 77]). */
function makeReadings() {
  return Array.from({ length: 48 }, (_, i) => ({
    timestamp: isoAtHour(i),
    // Below the [20, 28] setpoint during first 6 hours → out-of-range excursion.
    temperature: i < 6 ? 19.3 : 22.0,
    humidity: 60,
    co2: 400,
    anomaly_score: i === 2 ? 0.85 : 0.01,
  }));
}

/** Episode: temperature drop with peak_value 19.3 (below setpoint 20–28). */
const EPISODE = {
  episode_id: "temperature:statistical:drop:2026-06-01T02:00:00+00:00",
  channel: "temperature",
  detector: "statistical",
  direction: "drop",
  start_ts: "2026-06-01T00:00:00+00:00",
  end_ts: "2026-06-01T05:00:00+00:00",
  duration_seconds: 18000,
  sample_count: 6,
  peak_score: 0.85,
  peak_ts: "2026-06-01T02:00:00+00:00",
  severity: "warning",
  first_value: 19.3,
  last_value: 19.3,
  peak_value: 19.3,
  trend: "flat",
  movement: "steady",
  slope_per_hour: 0,
  affected_sensors: ["temperature"],
  headline: "Unexpected temperature drop: 19.3°C (20°C lower bound), flat over 5 h",
  is_truncated_start: false,
  is_truncated_end: false,
  samples: [
    {
      timestamp: "2026-06-01T02:00:00+00:00",
      anomaly_score: 0.85,
      affected_sensors: ["temperature"],
      reasoning: "Unexpected temperature drop: 19.3°C (20°C lower bound)",
      values: { temperature: 19.3 },
      detected_severity: "warning",
    },
  ],
  frozen_sample_count: null,
  raw_slot_count: null,
};

const EPISODES_PAYLOAD = {
  episodes: [EPISODE],
  episode_count: 1,
  anomaly_count: 1,
  threshold: 0.15,
  score_scale_version: 2,
};

test.beforeEach(async ({ page }) => {
  const rows = makeReadings();
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows, count: rows.length } }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            timestamp: isoAtHour(2),
            anomaly_score: 0.85,
            fault_type: null,
            reasoning: null,
            affected_sensors: ["temperature"],
            incident_id: null,
          },
        ],
        count: 1,
        anomaly_count: 1,
      },
    }),
  );
  await page.route("**/api/bff/anomalies/episodes**", (route) =>
    route.fulfill({ json: EPISODES_PAYLOAD }),
  );
  await page.route("**/api/bff/readings/anomalies-in-window**", (route) =>
    route.fulfill({
      json: {
        rows: [{ timestamp: isoAtHour(2), anomaly_score: 0.85, temperature: 19.3 }],
        anomaly_count: 1,
        total_count: 48,
      },
    }),
  );
});

/**
 * Part 2A: setpoint context appears on the episode summary.
 *
 * The peak_value 19.3 is below setpoint [20, 28], so the context line must
 * read "19.3 °C — 0.7 below setpoint 20–28".
 */
test("episode summary shows setpoint context for out-of-range peak value", async ({ page }) => {
  await page.goto(`/anomalies?${WINDOW}`);

  await expect(page.getByTestId("episode-table")).toBeVisible({ timeout: 5000 });

  const summary = page.getByTestId("episode-summary").first();
  await expect(summary).toBeVisible();

  // The setpoint context line should appear inside the episode summary.
  const ctxLine = page.getByTestId("episode-setpoint-context");
  await expect(ctxLine).toBeVisible();
  // 19.3 is 0.7 below the lower bound of 20.
  await expect(ctxLine).toContainText("19.3");
  await expect(ctxLine).toContainText("below setpoint 20–28");
});

/**
 * Part 2A: setpoint context appears on a sample row when expanded.
 */
test("expanded episode sample shows setpoint context", async ({ page }) => {
  await page.goto(`/anomalies?${WINDOW}`);

  await expect(page.getByTestId("episode-table")).toBeVisible({ timeout: 5000 });

  // Expand the episode to see its raw samples.
  await page.getByTestId("episode-summary").first().click();
  await expect(page.getByTestId("episode-sample-row").first()).toBeVisible();

  const sampleCtx = page.getByTestId("sample-setpoint-context").first();
  await expect(sampleCtx).toBeVisible();
  await expect(sampleCtx).toContainText("below setpoint 20–28");
});

/**
 * Part 2B: the "Out of setpoint range" section appears with excursion rows.
 *
 * Readings rows with temperature 19.3°C (hours 0-5, below setpoint 20-28)
 * should produce at least one excursion row. The detector flagged a temperature
 * anomaly in that same window, so no "likely the same event" note should appear
 * (the excursion section is for readings NOT flagged by the detector — but a
 * related-channel coupling with a different channel still adds the note).
 */
test("out-of-setpoint excursions section lists temperature readings not captured by the detector", async ({
  page,
}) => {
  await page.goto(`/anomalies?${WINDOW}`);

  await expect(page.getByTestId("setpoint-excursions-section")).toBeVisible({ timeout: 5000 });

  // There should be at least one excursion row for the temperature readings.
  const rows = page.getByTestId("excursion-row");
  await expect(rows.first()).toBeVisible();
  // getChannelLabel("temperature") returns "Air Temp" per CHANNEL_META.
  await expect(rows.first()).toContainText("Air Temp");
  // The worst value column should indicate the out-of-range state.
  await expect(rows.first()).toContainText("below setpoint");
});

/**
 * Part 2B: empty-state renders correctly when all readings are within setpoints.
 */
test("excursions section shows empty state when no readings are out of setpoint range", async ({
  page,
}) => {
  // Override readings with temperature always within [20, 28].
  const inRangeRows = Array.from({ length: 48 }, (_, i) => ({
    timestamp: isoAtHour(i),
    temperature: 22.0,
    humidity: 60,
    co2: 400,
    anomaly_score: i === 2 ? 0.85 : 0.01,
  }));
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: { rows: inRangeRows, count: inRangeRows.length } }),
  );
  await page.route("**/api/bff/readings/window**", (route) =>
    route.fulfill({ json: { rows: inRangeRows, count: inRangeRows.length } }),
  );

  await page.goto(`/anomalies?${WINDOW}`);

  await expect(page.getByTestId("setpoint-excursions-section")).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId("setpoint-excursions-empty")).toBeVisible();
  await expect(page.getByTestId("excursion-row")).toHaveCount(0);
});
