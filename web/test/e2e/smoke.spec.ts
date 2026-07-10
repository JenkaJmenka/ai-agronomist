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
