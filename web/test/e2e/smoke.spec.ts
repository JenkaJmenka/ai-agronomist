import { expect, test } from "@playwright/test";

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

test("tenant selector updates the URL", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("tenant-select").selectOption("tenant_2");
  await expect(page).toHaveURL(/tenant=tenant_2/);
  await page.getByTestId("greenhouse-select").selectOption("gh_B");
  await expect(page).toHaveURL(/gh=gh_B/);
});

test("quick range buttons write the window to the URL", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "7 d" }).click();
  await expect(page).toHaveURL(/from=.+to=.+/);
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

test("thresholds page: preview slider updates flagged counts and URL", async ({ page }) => {
  await page.goto("/thresholds");
  await expect(page.getByRole("heading", { name: "Threshold Management" })).toBeVisible();
  const slider = page.getByTestId("threshold-control").locator("input[type=range]");
  await slider.fill("0.3");
  await expect(page).toHaveURL(/preview=0.3/);
});

test("metrics page renders both islands", async ({ page }) => {
  await page.goto("/metrics");
  await expect(page.getByRole("heading", { name: "System Metrics" })).toBeVisible();
  await expect(page.getByText(/Detections \(last 24 h\)/i)).toBeVisible();
  await expect(page.getByText("Pattern distribution", { exact: false })).toBeVisible();
});
