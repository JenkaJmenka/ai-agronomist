import { expect, test } from "@playwright/test";

/**
 * P4 Upload & Operations flows — every BFF call is mocked so e2e never
 * touches a real backend (plan §8 P4 gates).
 */

const CSV_OK = "timestamp,temperature,humidity\n2026-06-01T00:00:00,21.5,60\n";

const MODEL_VERSIONS = [
  {
    model_id: "model-a",
    is_active: true,
    model_type: "lstm_vae",
    created_at: "2026-07-01T10:00:00Z",
  },
  {
    model_id: "model-b",
    is_active: false,
    model_type: "lstm_vae",
    created_at: "2026-07-02T10:00:00Z",
  },
];

test.beforeEach(async ({ page }) => {
  await page.route("**/api/bff/models/*/versions**", (route) =>
    route.fulfill({ json: MODEL_VERSIONS }),
  );
});

test("upload requires an explicit two-step confirm before POSTing", async ({ page }) => {
  let uploadRequests = 0;
  await page.route("**/api/bff/inference/upload-async", (route) => {
    uploadRequests += 1;
    return route.fulfill({
      json: { job_id: "job-e2e-1", portion_count: 3, poll_url: "/api/jobs/job-e2e-1" },
    });
  });

  await page.goto("/operations");
  await page.getByTestId("upload-file-input").setInputFiles({
    name: "readings.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(CSV_OK),
  });
  await page.getByLabel("Ingest data", { exact: true }).check();
  await page.getByTestId("upload-submit").click();

  // Step 1 arms only — no request yet.
  await expect(page.getByRole("alertdialog", { name: "Confirm upload" })).toBeVisible();
  expect(uploadRequests).toBe(0);

  await page.getByTestId("upload-confirm").click();
  await expect(page.getByText("Upload job accepted:")).toBeVisible();
  await expect(page.getByText("job-e2e-1")).toBeVisible();
  expect(uploadRequests).toBe(1);
});

test("cancelling the upload confirm never POSTs", async ({ page }) => {
  let uploadRequests = 0;
  await page.route("**/api/bff/inference/**", (route) => {
    uploadRequests += 1;
    return route.fulfill({ json: {} });
  });

  await page.goto("/operations");
  await page.getByTestId("upload-file-input").setInputFiles({
    name: "readings.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(CSV_OK),
  });
  await page.getByTestId("upload-submit").click();
  await page.getByTestId("upload-cancel").click();
  await expect(page.getByTestId("upload-submit")).toBeEnabled();
  expect(uploadRequests).toBe(0);
});

test("invalid CSV blocks the upload with a visible error", async ({ page }) => {
  await page.goto("/operations");
  await page.getByTestId("upload-file-input").setInputFiles({
    name: "header-only.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("timestamp,temperature,humidity\n"),
  });
  await expect(page.getByText(/no data rows/i)).toBeVisible();
  await expect(page.getByTestId("upload-submit")).toBeDisabled();
});

test("clear data fires only after the second confirm click", async ({ page }) => {
  let deleteRequests = 0;
  await page.route("**/api/bff/upload/data**", (route) => {
    deleteRequests += 1;
    return route.fulfill({ json: { status: "ok" } });
  });

  await page.goto("/operations");
  await page.getByTestId("clear-data-trigger").click();
  await expect(page.getByText(/permanently delete ALL data/)).toBeVisible();
  expect(deleteRequests).toBe(0);

  await page.getByTestId("clear-data-confirm").click();
  await expect(page.getByText(/All data cleared/)).toBeVisible();
  expect(deleteRequests).toBe(1);
});

test("model registry lists versions and activates an inactive one", async ({ page }) => {
  let activateRequests = 0;
  await page.route("**/api/bff/models/*/activate", (route) => {
    activateRequests += 1;
    return route.fulfill({ json: { status: "success" } });
  });

  await page.goto("/operations");
  await expect(page.getByText("Active model:")).toBeVisible();
  await expect(page.getByRole("list").getByText("model-a")).toBeVisible();
  await expect(page.getByText("active", { exact: true })).toBeVisible();

  await page.getByTestId("activate-model-b").click();
  await expect
    .poll(() => activateRequests, { message: "activate call reaches the BFF" })
    .toBe(1);
});
