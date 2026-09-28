import { expect, type Page, test } from "@playwright/test";

/** Fill the segmented DateTimeField from an ISO-like "YYYY-MM-DDTHH:mm". */
async function fillDateTimeField(page: Page, testId: string, value: string): Promise<void> {
  const [datePart, timePart] = value.split("T");
  const [year, month, day] = datePart.split("-");
  const [hour, minute] = timePart.split(":");
  const segments = { day, month, year, hour, minute };
  for (const [segment, digits] of Object.entries(segments)) {
    await page.getByTestId(`${testId}-${segment}`).fill(digits);
  }
}

/**
 * Register BFF stubs that all navigation targets need.
 *
 * Extracted from the `app boots` inline mocks so the nav smoke test shares
 * exactly one payload definition — one edit here covers both tests.
 *
 * Only the three routes that gate page rendering are included:
 * - readings/**  : prevents Monitoring/Climate/Gas/Anomalies/Thresholds from
 *                  reaching their `isError → throw` error-boundary path.
 * - config/**    : prevents ConfigurationView (Greenhouse Profile) from
 *                  throwing to the root Next.js error page ("This page
 *                  couldn't load") — that page has no segment error.tsx, so
 *                  the sidebar disappears and subsequent nav clicks time out.
 * - live/**      : avoids noise from the optional SSE probe on the Home page.
 */
async function setupBffMocks(page: Page): Promise<void> {
  await page.route("**/api/bff/readings/**", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            timestamp: "2026-07-18T12:00:00.000Z",
            temperature: 22,
            humidity: 60,
            co2: 400,
            light: 500,
            anomaly_score: 0.01,
          },
        ],
        count: 1,
        min_timestamp: "2026-07-18T12:00:00.000Z",
        max_timestamp: "2026-07-18T12:00:00.000Z",
      },
    }),
  );
  await page.route("**/api/bff/live/**", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/bff/config/**", (route) =>
    route.fulfill({
      json: {
        config: {
          crop: "tomato",
          day_window: { start_hour: 6, end_hour: 18 },
          co2_enrichment: false,
          setpoints: {},
          season_map: {},
        },
        sources: {},
      },
    }),
  );
}

/**
 * NAV_TARGETS drives the sidebar navigation smoke test.
 *
 * `heading` is the exact h1 text expected on the destination page — it may
 * differ from the nav label (e.g. "Thresholds" → "Threshold Management").
 * Keeping it explicit prevents the test from silently passing on an error
 * page whose own h1 would satisfy a level-only assertion.
 */
const NAV_TARGETS = [
  { label: "Monitoring", path: "/monitoring", heading: "Monitoring" },
  { label: "Climate Data", path: "/climate", heading: "Climate Data" },
  { label: "Gas Monitoring", path: "/gas", heading: "Gas Monitoring" },
  { label: "Anomaly Detection", path: "/anomalies", heading: "Anomaly Detection" },
  { label: "Thresholds", path: "/thresholds", heading: "Threshold Management" },
  { label: "System Metrics", path: "/metrics", heading: "System Metrics" },
  { label: "Greenhouse Profile", path: "/greenhouse-profile", heading: "Greenhouse Profile" },
  { label: "App Configuration", path: "/app-configuration", heading: "App Configuration" },
  { label: "Upload & Operations", path: "/operations", heading: "Upload & Operations" },
  { label: "Job Monitoring", path: "/jobs", heading: "Job Monitoring" },
  { label: "Operator Feedback", path: "/feedback", heading: "Operator Feedback" },
];

test("app boots and renders the Home page", async ({ page }) => {
  await setupBffMocks(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "AI Agronomist" })).toBeVisible();
  await expect(page.getByTestId("home-page")).toBeVisible();
  await expect(page.getByTestId("uplot-chart")).toBeVisible();
});

test("every sidebar entry navigates", async ({ page }) => {
  await setupBffMocks(page);
  await page.goto("/");
  for (const target of NAV_TARGETS) {
    await page.getByRole("link", { name: target.label, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${target.path}(\\?.*)?$`));

    // Guard: if the page crashed (no segment error.tsx → Next.js default error
    // page), fail on the broken target, not on the next target's sidebar click.
    // "This page couldn't load" is the h1 Next.js renders in that case; the
    // sidebar disappears with the layout, causing a 30 s timeout one step later.
    await expect(page.getByRole("heading", { name: "This page couldn't load" })).not.toBeVisible();

    // Assert the EXPECTED heading text, not merely that any h1 exists.
    // Without this specificity, the error-page h1 would make the assertion
    // pass on a crashed page, hiding the failure until the next target.
    await expect(page.getByRole("heading", { name: target.heading, level: 1 })).toBeVisible();
  }
});

test("monitoring family screens stay navigable after the first open", async ({ page }) => {
  // Regression: live chart remounts + URL rewrites used to cancel soft navigations.
  await page.route("**/api/bff/readings/**", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            timestamp: "2026-07-18T12:00:00.000Z",
            temperature: 22,
            humidity: 60,
            co2: 400,
            light: 500,
          },
        ],
        count: 1,
        min_timestamp: "2026-07-18T12:00:00.000Z",
        max_timestamp: "2026-07-18T12:00:00.000Z",
      },
    }),
  );
  await page.route("**/api/bff/live/events**", (route) => route.fulfill({ status: 204, body: "" }));
  // live=true in the URL triggers the one-shot resume-on-mount effect
  // (useLiveReadings) on every page in this nav chain — unmocked it would
  // fall through to the BFF proxy's dead-port 502 path instead of a
  // controlled response.
  await page.route("**/api/bff/live/resume**", (route) =>
    route.fulfill({
      json: { resumed: true, tenant_id: "tenant_1", greenhouse_id: "gh_A" },
    }),
  );

  await page.goto("/monitoring?live=true&refresh=10");
  await expect(page.getByRole("heading", { name: "Monitoring" })).toBeVisible();

  await page.getByRole("link", { name: "Climate Data", exact: true }).click();
  await expect(page).toHaveURL(/\/climate/);
  await expect(page.getByRole("heading", { name: "Climate Data" })).toBeVisible();

  await page.getByRole("link", { name: "Gas Monitoring", exact: true }).click();
  await expect(page).toHaveURL(/\/gas/);
  await expect(page.getByRole("heading", { name: "Gas Monitoring" })).toBeVisible();

  await page.getByRole("link", { name: "Monitoring", exact: true }).click();
  await expect(page).toHaveURL(/\/monitoring/);
  await expect(page.getByRole("heading", { name: "Monitoring" })).toBeVisible();
});

test("sidebar preserves period, live, refresh, and threshold across pages", async ({ page }) => {
  await page.goto(
    "/monitoring?from=2026-06-01T00:00:00.000Z&to=2026-06-02T00:00:00.000Z&live=true&refresh=60&threshold=0.25",
  );
  await page.getByRole("link", { name: "Anomaly Detection", exact: true }).click();
  await expect(page).toHaveURL(/\/anomalies/);
  await expect(page).toHaveURL(/from=2026-06-01/);
  await expect(page).toHaveURL(/to=2026-06-02/);
  await expect(page).toHaveURL(/live=true/);
  await expect(page).toHaveURL(/refresh=60/);
  await expect(page).toHaveURL(/threshold=0.25/);

  await page.getByRole("link", { name: "Climate Data", exact: true }).click();
  await expect(page).toHaveURL(/\/climate/);
  await expect(page).toHaveURL(/live=true/);
  await expect(page).toHaveURL(/threshold=0.25/);
  await expect(page.getByTestId("shared-view-controls")).toBeVisible();
  await expect(page.getByTestId("live-toggle")).toHaveAttribute("data-live", "true");
});

test("greenhouse selector updates the URL", async ({ page }) => {
  // Tenant is no longer client-selectable — the BFF derives it from the
  // session (security phase 2), so `TenantSelector` was removed and only
  // the greenhouse picker (`GreenhouseSelector`) remains.
  await page.goto("/");
  await expect(page.getByTestId("tenant-select")).toHaveCount(0);
  await page.getByTestId("greenhouse-select").selectOption("gh_B");
  await expect(page).toHaveURL(/gh=gh_B/);
});

test("period select writes the window to the URL", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("period-select").selectOption("7d");
  await expect(page).toHaveURL(/from=.+to=.+/);
});

test("custom range form writes an explicit window to the URL", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("period-select").selectOption("custom");
  await fillDateTimeField(page, "custom-range-from", "2026-06-01T12:00");
  await fillDateTimeField(page, "custom-range-to", "2026-06-15T12:30");
  await page.getByTestId("custom-range-apply").click();
  // Contract: DateTimeField constructs dates with new Date(year, month-1, day, hour, minute),
  // which uses the browser's local timezone. nuqs then serialises via .toISOString() (UTC) into
  // the URL. Evaluate the expected instants inside the browser so the assertion is independent
  // of the Node.js process TZ — the two can diverge when TZ is overridden in the test runner
  // but the browser resolves timezone from a different source (e.g. OS vs env-var on macOS).
  const [expectedFromMs, expectedToMs] = await page.evaluate(() => [
    new Date(2026, 5, 1, 12, 0).getTime(),
    new Date(2026, 5, 15, 12, 30).getTime(),
  ]);
  // Wait until the URL carries the exact submitted window before asserting.
  // Cannot use /from=.+to=.+/ here — a `from` param is already present in the
  // default now-derived window, so that pattern resolves immediately without
  // waiting for nuqs to serialise the submitted values.  The submitted instants
  // always land on a whole minute (milliseconds === 0), so they can never be
  // satisfied by the live `now` timestamp whose ms component is non-zero.
  const expectedFromIso = new Date(expectedFromMs).toISOString();
  const expectedToIso = new Date(expectedToMs).toISOString();
  await page.waitForURL(
    (url) =>
      url.searchParams.get("from") === expectedFromIso &&
      url.searchParams.get("to") === expectedToIso,
  );
  const params = new URL(page.url()).searchParams;
  expect(new Date(params.get("from") ?? "").getTime()).toBe(expectedFromMs);
  expect(new Date(params.get("to") ?? "").getTime()).toBe(expectedToMs);
});

test("custom range apply is disabled when from is not before to", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("period-select").selectOption("custom");
  await fillDateTimeField(page, "custom-range-from", "2026-06-15T12:00");
  await fillDateTimeField(page, "custom-range-to", "2026-06-01T12:00");
  await expect(page.getByTestId("custom-range-apply")).toBeDisabled();
  // Scoped to the form — Next's route announcer is also role="alert".
  await expect(page.getByTestId("custom-range-form").getByRole("alert")).toContainText(
    "must be before",
  );
});

test("custom range year segment caps at 4 digits and advances to hours", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("period-select").selectOption("custom");
  const year = page.getByTestId("custom-range-from-year");
  await year.click();
  await year.pressSequentially("20261");
  // 5th digit never lands in the year — the caret has moved on to hours.
  await expect(year).toHaveValue("2026");
  await expect(page.getByTestId("custom-range-from-hour")).toBeFocused();
  await expect(page.getByTestId("custom-range-from-hour")).toHaveValue("1");
});

test("anomalies page: threshold control and view toggle render", async ({ page }) => {
  await page.goto("/anomalies");
  await expect(page.getByRole("heading", { name: "Anomaly Detection" })).toBeVisible();
  await expect(page.getByTestId("threshold-control")).toBeVisible();
  await page.getByRole("tab", { name: "Table view" }).click();
  await expect(page.getByRole("tab", { name: "Table view" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("anomalies threshold writes to the URL", async ({ page }) => {
  await page.goto("/anomalies");
  const slider = page.getByTestId("threshold-control").locator("input[type=range]");
  await slider.fill("0.25");
  await expect(page).toHaveURL(/threshold=0.25/);
});

test("thresholds page: threshold slider updates flagged counts and URL", async ({ page }) => {
  // The slider only renders over scored rows — mock the BFF so the test
  // is backend-independent (CI has no API; never rely on live data).
  const scoredRows = Array.from({ length: 48 }, (_, i) => ({
    timestamp: new Date(Date.UTC(2026, 5, 1, i)).toISOString(),
    temperature: 22,
    anomaly_score: i % 8 === 0 ? 0.4 : 0.05,
  }));
  await page.route("**/api/bff/readings/recent**", (route) =>
    route.fulfill({
      json: {
        rows: scoredRows,
        count: scoredRows.length,
        min_timestamp: scoredRows[0].timestamp,
        max_timestamp: scoredRows.at(-1)?.timestamp,
      },
    }),
  );

  await page.goto("/thresholds");
  await expect(page.getByRole("heading", { name: "Threshold Management" })).toBeVisible();
  const slider = page.getByTestId("threshold-control").locator("input[type=range]");
  await slider.fill("0.3");
  await expect(page).toHaveURL(/threshold=0.3/);
  // Slider recomputes flagged counts client-side over the mock data.
  await expect(page.getByText("Flagged at threshold (0.300)")).toBeVisible();
});

test("calendar dropdown picks a day into the segmented field", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("period-select").selectOption("custom");
  await fillDateTimeField(page, "custom-range-from", "2026-06-01T09:30");
  await page.getByTestId("custom-range-from-calendar-toggle").click();
  const calendar = page.getByTestId("custom-range-from-calendar");
  await expect(calendar).toBeVisible();
  await calendar.getByRole("button", { name: "15", exact: true }).click();
  // Picking a day updates the date segments, keeps the typed time, and closes.
  await expect(page.getByTestId("custom-range-from-day")).toHaveValue("15");
  await expect(page.getByTestId("custom-range-from-hour")).toHaveValue("09");
  await expect(calendar).not.toBeVisible();
});

test("theme toggle flips to dark and persists across reload", async ({ page }) => {
  await page.goto("/");
  // Playwright defaults to a light color scheme, so the first flip is → dark.
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("metrics page renders both islands", async ({ page }) => {
  await page.goto("/metrics");
  await expect(page.getByRole("heading", { name: "System Metrics" })).toBeVisible();
  await expect(page.getByText(/Detections \(last 24 h\)/i)).toBeVisible();
  await expect(page.getByText("Pattern distribution", { exact: false })).toBeVisible();
});
