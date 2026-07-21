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

const NAV_TARGETS = [
  { label: "Monitoring", path: "/monitoring" },
  { label: "Climate Data", path: "/climate" },
  { label: "Gas Monitoring", path: "/gas" },
  { label: "Anomaly Detection", path: "/anomalies" },
  { label: "Thresholds", path: "/thresholds" },
  { label: "System Metrics", path: "/metrics" },
  { label: "Upload & Operations", path: "/operations" },
  { label: "Job Monitoring", path: "/jobs" },
  { label: "Operator Feedback", path: "/feedback" },
];

test("app boots and renders the Home page", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "AI Agronomist" })).toBeVisible();
  await expect(page.getByTestId("uplot-chart")).toBeVisible();
});

test("every sidebar entry navigates", async ({ page }) => {
  await page.goto("/");
  for (const target of NAV_TARGETS) {
    await page.getByRole("link", { name: target.label, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${target.path}(\\?.*)?$`));
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  }
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

test("tenant selector updates the URL", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("tenant-select").selectOption("tenant_2");
  await expect(page).toHaveURL(/tenant=tenant_2/);
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
  await expect(page).toHaveURL(/from=.+to=.+/);
  // Segment values are local time; compare instants, not strings.
  const params = new URL(page.url()).searchParams;
  expect(new Date(params.get("from") ?? "").getTime()).toBe(new Date("2026-06-01T12:00").getTime());
  expect(new Date(params.get("to") ?? "").getTime()).toBe(new Date("2026-06-15T12:30").getTime());
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
