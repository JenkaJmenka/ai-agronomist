import { expect, test } from "@playwright/test";

/** P4 Job Monitoring — SSE progress + terminate, all BFF calls mocked. */

const RUNNING_JOB = {
  id: "job-run",
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  job_type: "upload_csv",
  status: "running",
  total_portions: 4,
  completed_portions: 1,
  failed_portions: 0,
  progress_pct: 25.0,
  created_at: "2026-07-11T10:00:00Z",
};

const DONE_JOB = {
  ...RUNNING_JOB,
  id: "job-done",
  status: "completed",
  completed_portions: 4,
  progress_pct: 100.0,
  finished_at: "2026-07-11T11:00:00Z",
};

function sseBody(events: object[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}

test("job list renders live SSE progress through the BFF relay", async ({ page }) => {
  await page.route("**/api/bff/jobs?**", (route) =>
    route.fulfill({ json: { jobs: [RUNNING_JOB, DONE_JOB] } }),
  );
  await page.route("**/api/bff/jobs/job-run/events", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: sseBody([
        { status: "running", progress_pct: 50.0, completed_portions: 2, total_portions: 4 },
        { status: "completed", progress_pct: 100.0, completed_portions: 4, total_portions: 4 },
      ]),
    }),
  );

  await page.goto("/jobs");
  const runningCard = page.getByTestId("job-job-run");
  await expect(runningCard).toBeVisible();
  // Terminal SSE event flips the chip without any list refetch.
  await expect(runningCard.getByText("completed")).toBeVisible();

  const doneCard = page.getByTestId("job-job-done");
  await expect(doneCard.getByText("completed")).toBeVisible();
  await expect(doneCard.getByText(/Finished 2026-07-11/)).toBeVisible();
});

test("terminate posts a cancel through the BFF", async ({ page }) => {
  let cancelRequests = 0;
  await page.route("**/api/bff/jobs?**", (route) =>
    route.fulfill({ json: { jobs: [RUNNING_JOB] } }),
  );
  // Keep the job "running": an empty SSE stream that never resolves progress.
  await page.route("**/api/bff/jobs/job-run/events", (route) =>
    route.fulfill({ contentType: "text/event-stream", body: ": keep-alive\n\n" }),
  );
  await page.route("**/api/bff/jobs/job-run/cancel", (route) => {
    cancelRequests += 1;
    return route.fulfill({ json: { ...RUNNING_JOB, status: "failed" } });
  });

  await page.goto("/jobs");
  await page.getByTestId("terminate-job-run").click();
  await expect.poll(() => cancelRequests).toBe(1);
});

test("empty job list shows the explicit empty state", async ({ page }) => {
  await page.route("**/api/bff/jobs?**", (route) => route.fulfill({ json: { jobs: [] } }));
  await page.goto("/jobs");
  await expect(page.getByText(/No recent jobs/)).toBeVisible();
});
