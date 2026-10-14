import { expect, test } from "@playwright/test";

/** Episode merging: the Table view collapses contiguous 5-minute samples into
 *  one event per row, with the raw samples behind an expand. All BFF mocked. */

const EPISODE_START = "2026-12-25T09:55:00+00:00";
const EPISODE_END = "2026-12-25T10:40:00+00:00";

function isoAtHour(hour: number): string {
  const base = new Date("2026-12-25T00:00:00Z").getTime();
  return new Date(base + hour * 3_600_000).toISOString().replace(".000Z", "+00:00");
}

/** Readings envelope — drives the window bounds the episode query uses. */
function mockRecent() {
  const rows = Array.from({ length: 48 }, (_, i) => ({
    timestamp: isoAtHour(i),
    temperature: 21,
    humidity: 60,
    anomaly_score: i % 24 === 0 ? 0.9 : null,
  }));
  return {
    rows,
    count: rows.length,
    min_timestamp: rows[0].timestamp,
    max_timestamp: rows.at(-1)?.timestamp ?? null,
  };
}

/** The real shape from the CSV export: a 10-sample humidity drop bottoming
 *  at 29.0%, plus a separate 3-sample temperature drop that must not merge. */
const EPISODES_PAYLOAD = {
  episodes: [
    {
      episode_id: `humidity:statistical:drop:${EPISODE_START}`,
      channel: "humidity",
      detector: "statistical",
      direction: "drop",
      start_ts: EPISODE_START,
      end_ts: EPISODE_END,
      duration_seconds: 2700,
      sample_count: 10,
      peak_score: 0.85,
      peak_ts: "2026-12-25T10:00:00+00:00",
      severity: "warning",
      first_value: 32.0,
      last_value: 30.0,
      peak_value: 29.0,
      trend: "worsening",
      movement: "falling",
      slope_per_hour: -2.7,
      affected_sensors: ["humidity"],
      headline: "Unexpected humidity drop: 29.0% (50% lower bound), worsening over 45 min",
      is_truncated_start: false,
      is_truncated_end: false,
      samples: [
        {
          timestamp: "2026-12-25T10:00:00+00:00",
          anomaly_score: 0.85,
          affected_sensors: ["humidity"],
          reasoning: "Unexpected humidity drop: 32.0% (50% lower bound)",
          values: { humidity: 32.0 },
          detected_severity: "warning",
        },
        {
          timestamp: "2026-12-25T09:55:00+00:00",
          anomaly_score: 0.848,
          affected_sensors: ["humidity"],
          reasoning: "Unexpected humidity drop: 32.0% (50% lower bound)",
          values: { humidity: 32.0 },
          detected_severity: "warning",
        },
      ],
      frozen_sample_count: null,
      raw_slot_count: null,
    },
    {
      episode_id: "temperature:statistical:drop:2026-12-25T09:10:00+00:00",
      channel: "temperature",
      detector: "statistical",
      direction: "drop",
      start_ts: "2026-12-25T09:10:00+00:00",
      end_ts: "2026-12-25T09:20:00+00:00",
      duration_seconds: 600,
      sample_count: 3,
      peak_score: 0.794,
      peak_ts: "2026-12-25T09:10:00+00:00",
      severity: "warning",
      first_value: 19.3,
      last_value: 19.5,
      peak_value: 19.3,
      trend: "easing",
      movement: "rising",
      slope_per_hour: 0.6,
      affected_sensors: ["temperature", "humidity"],
      headline: "Unexpected temperature drop: 19.3°C (20°C lower bound), easing over 10 min",
      is_truncated_start: false,
      is_truncated_end: false,
      samples: [
        {
          timestamp: "2026-12-25T09:10:00+00:00",
          anomaly_score: 0.794,
          affected_sensors: ["temperature", "humidity"],
          reasoning: "Unexpected temperature drop: 19.3°C (20°C lower bound)",
          values: { temperature: 19.3 },
          detected_severity: "warning",
        },
      ],
      frozen_sample_count: null,
      raw_slot_count: null,
    },
  ],
  episode_count: 2,
  anomaly_count: 13,
  threshold: 0.5,
  score_scale_version: 2,
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({ json: mockRecent() }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({ json: { rows: [], count: 0, anomaly_count: 13 } }),
  );
  await page.route("**/api/bff/anomalies/episodes**", (route) =>
    route.fulfill({ json: EPISODES_PAYLOAD }),
  );
  // GroupedAnomalies routes on this cheap count, then renders the client path
  // from these rows. total_count is required by the response schema.
  await page.route("**/api/bff/readings/anomalies-in-window**", (route) =>
    route.fulfill({
      json: {
        rows: EPISODES_PAYLOAD.episodes.flatMap((episode) =>
          episode.samples.map((sample) => ({
            timestamp: sample.timestamp,
            anomaly_score: sample.anomaly_score,
            temperature: 19.3,
            humidity: 32,
          })),
        ),
        anomaly_count: 13,
        total_count: 48,
      },
    }),
  );
});

test("falls back to per-sample rows when the episodes endpoint is unavailable", async ({
  page,
}) => {
  // Production regression: web and api deploy independently, so the UI shipped
  // ahead of /api/anomalies/episodes and every anomalies page 500'd. The page
  // must degrade to the per-sample view, not become a dead end.
  await page.route("**/api/bff/anomalies/episodes**", (route) =>
    route.fulfill({ status: 500, json: { message: "An internal server error occurred." } }),
  );
  await page.route("**/api/bff/anomalies/enriched**", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            timestamp: "2026-12-25T10:00:00+00:00",
            anomaly_score: 0.85,
            fault_type: "spike_fault",
            reasoning: "Unexpected humidity drop: 32.0% (50% lower bound)",
            affected_sensors: ["humidity"],
            incident_id: null,
          },
        ],
        count: 1,
        anomaly_count: 13,
      },
    }),
  );

  await page.goto("/anomalies");

  await expect(page.getByTestId("episode-fallback")).toBeVisible();
  await expect(page.getByText("Episode merging is unavailable")).toBeVisible();
  // The operator still sees their anomalies rather than an error.
  await expect(page.getByTestId("fault-type").first()).toBeVisible();
  await expect(page.getByText("Could not load anomaly episodes")).toBeHidden();
});

test("the landing view shows merged episodes, not one row per 5-min sample", async ({ page }) => {
  // Regression: episodes first shipped only into Table view, so the default
  // Graphical view still listed every sample and the merge was invisible.
  await page.goto("/anomalies");

  await expect(page.getByTestId("episode-table")).toBeVisible();
  await expect(page.getByTestId("episode-summary")).toHaveCount(2);
  await expect(page.getByText("Anomaly episodes")).toBeVisible();
  // The flat per-sample table must not be what greets the operator.
  await expect(page.getByText("Top anomalies")).toBeHidden();
});

test("samples toggle restores the flat per-sample tables", async ({ page }) => {
  await page.goto("/anomalies");
  await page.getByTestId("chart-anomaly-mode").getByRole("button", { name: "Samples" }).click();

  await expect(page.getByTestId("episode-table")).toBeHidden();
  await expect(page.getByText("Top anomalies")).toBeVisible();
  await expect(page.getByText(/All anomalies/)).toBeVisible();
});

test("expanding an episode reveals its samples without an explicit zoom", async ({ page }) => {
  await page.goto("/anomalies");
  await expect(page.getByTestId("episode-table")).toContainText('"Zoom to span" focuses the chart');

  // Regression: expanding used to zoom the chart too, silently replacing the
  // operator's date range. Expanding must stay a pure disclosure action.
  await page.getByTestId("episode-summary").first().click();
  await expect(page.getByTestId("episode-sample-row").first()).toBeVisible();
});

test('the explicit "Zoom to span" control does not expand the episode', async ({ page }) => {
  await page.goto("/anomalies");

  await page.getByTestId("episode-zoom-button").first().click();

  // Zooming is a distinct act from disclosure — the samples stay collapsed.
  await expect(page.getByTestId("episode-sample-row").first()).toBeHidden();
});

test("table view merges contiguous samples into one episode per event", async ({ page }) => {
  await page.goto("/anomalies");
  await page.getByRole("tab", { name: "Table view" }).click();

  const table = page.getByTestId("episode-table");
  await expect(table).toBeVisible();

  // Two events, not the 13 five-minute samples they merge.
  const summaries = page.getByTestId("episode-summary");
  await expect(summaries).toHaveCount(2);
  await expect(table).toContainText("2 episodes merged from 13 samples");
});

test("episode summary shows span, duration, extremum and trend", async ({ page }) => {
  await page.goto("/anomalies");
  await page.getByRole("tab", { name: "Table view" }).click();

  const humidity = page.getByTestId("episode-summary").first();
  await expect(humidity).toContainText("2026-12-25 09:55 → 10:40");
  await expect(humidity).toContainText("45 min");
  await expect(humidity).toContainText("10 samples");
  // The low reached, not the reading it happened to end on.
  await expect(humidity).toContainText("32.0% → 29.0%");
  await expect(humidity).toContainText("worsening");
});

test("the adjacent temperature drop stays a separate episode", async ({ page }) => {
  await page.goto("/anomalies");
  await page.getByRole("tab", { name: "Table view" }).click();

  const temperature = page.getByTestId("episode-summary").nth(1);
  await expect(temperature).toContainText("Temperature drop");
  await expect(temperature).toContainText("2026-12-25 09:10 → 09:20");
  await expect(temperature).toContainText("3 samples");
});

test("expanding an episode reveals the raw 5-minute samples", async ({ page }) => {
  await page.goto("/anomalies");
  await page.getByRole("tab", { name: "Table view" }).click();

  // Samples are inside a collapsed <details> until the summary is clicked.
  await page.getByTestId("episode-summary").first().click();

  const samples = page.getByTestId("episode-sample-row");
  await expect(samples.first()).toContainText("2026-12-25 10:00:00");
  await expect(samples.first()).toContainText("0.850");
  // The per-sample reasoning still quotes that sample's own value.
  await expect(samples.first()).toContainText("32.0%");
});

test("grouped view remains reachable from the table view", async ({ page }) => {
  await page.goto("/anomalies");
  await page.getByRole("tab", { name: "Table view" }).click();
  await page.getByRole("button", { name: "Grouped" }).click();

  await expect(page.getByTestId("episode-table")).toBeHidden();
  await expect(page.getByTestId("group-by")).toBeVisible();
});

test("a stuck-fault episode with no matching table row shows duration unknown, not a fabricated zero", async ({
  page,
}) => {
  // Regression: duration_seconds went from a required number to nullable so
  // a pre-migration-026 stuck fault can report "unknown" instead of a false
  // 0.0. An un-widened zod schema throws here and the whole page 500s.
  await page.route("**/api/bff/anomalies/episodes**", (route) =>
    route.fulfill({
      json: {
        episodes: [
          {
            episode_id: "temperature:stuck:2026-12-25T09:00:00+00:00",
            channel: "temperature",
            detector: "stuck",
            direction: null,
            start_ts: "2026-12-25T09:00:00+00:00",
            end_ts: "2026-12-25T09:00:00+00:00",
            duration_seconds: null,
            sample_count: 1,
            peak_score: 0.95,
            peak_ts: "2026-12-25T09:00:00+00:00",
            severity: "critical",
            first_value: 21.0,
            last_value: 21.0,
            peak_value: 21.0,
            trend: "flat",
            movement: "steady",
            slope_per_hour: null,
            affected_sensors: ["temperature"],
            headline: "Temperature stuck at 21.0\u00b0C",
            is_truncated_start: false,
            is_truncated_end: false,
            samples: [],
            frozen_sample_count: null,
            raw_slot_count: null,
          },
        ],
        episode_count: 1,
        anomaly_count: 1,
        threshold: 0.5,
        score_scale_version: 2,
      },
    }),
  );

  await page.goto("/anomalies");
  await page.getByRole("tab", { name: "Table view" }).click();

  const summary = page.getByTestId("episode-summary").first();
  await expect(summary).toBeVisible();
  await expect(summary).toContainText("duration unknown");
});
