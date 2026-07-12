import { expect, test } from "@playwright/test";

/** P5 Operator Feedback — optimistic verdict + rollback, BFF mocked. */

const INCIDENTS = [
  {
    id: "inc-1",
    detected_at: "2026-07-10T08:00:00Z",
    anomaly_score: 0.91,
    fault_type: "spike_fault",
    root_cause: "Fan failure",
    root_cause_details: {
      pattern_type: "spike",
      affected_sensors: ["temperature"],
      hint_summary: "Unexpected temperature spike detected",
    },
  },
  {
    id: "inc-2",
    detected_at: "2026-07-10T09:00:00Z",
    anomaly_score: 0.35,
    fault_type: "calibration_drift",
    root_cause_details: { affected_sensors: ["humidity"] },
  },
];

const VERDICT_OK = {
  status: "recorded",
  incident_id: "inc-1",
  learning_triggered: false,
  high_quality_feedback_count: 3,
  progress_to_retrain: "3/10",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/bff/feedback/learning-progress/**", (route) =>
    route.fulfill({
      json: {
        customer_id: "tenant_1",
        progress_percent: 30,
        samples_needed: 7,
        high_quality_count: 3,
        total_feedback: 5,
        last_retrain: null,
      },
    }),
  );
});

test("verdict removes the incident optimistically and reconciles", async ({ page }) => {
  let queueCalls = 0;
  await page.route("**/api/bff/incidents/open**", (route) => {
    queueCalls += 1;
    // After the verdict settles the refetch returns the shrunken queue.
    const jobs = queueCalls === 1 ? INCIDENTS : [INCIDENTS[1]];
    return route.fulfill({ json: jobs });
  });
  await page.route("**/api/bff/feedback/mark-anomaly", async (route) => {
    // Slow response: the optimistic removal must show BEFORE this resolves.
    await new Promise((resolve) => setTimeout(resolve, 700));
    return route.fulfill({ json: VERDICT_OK });
  });

  await page.goto("/feedback");
  await expect(page.getByTestId("incident-inc-1")).toBeVisible();
  await expect(page.getByTestId("learning-progress")).toBeVisible();

  await page.getByTestId("label-inc-1").click();
  const submitTime = Date.now();
  await page.getByTestId("verdict-submit").click();

  // Optimistic: gone well before the 700 ms server response.
  await expect(page.getByTestId("incident-inc-1")).toBeHidden();
  expect(Date.now() - submitTime).toBeLessThan(700);

  // Reconcile: server result banner appears after the response lands.
  await expect(page.getByTestId("verdict-result")).toContainText("recorded");
  await expect(page.getByTestId("incident-inc-2")).toBeVisible();
});

test("failed verdict rolls the incident back into the queue", async ({ page }) => {
  await page.route("**/api/bff/incidents/open**", (route) => route.fulfill({ json: INCIDENTS }));
  await page.route("**/api/bff/feedback/mark-anomaly", (route) =>
    route.fulfill({ status: 500, json: { detail: "learner unavailable" } }),
  );

  await page.goto("/feedback");
  await page.getByTestId("label-inc-1").click();
  await page.getByTestId("verdict-submit").click();

  await expect(page.getByText(/rolled back/)).toBeVisible();
  await expect(page.getByTestId("incident-inc-1")).toBeVisible();
});

test("empty queue shows the explicit empty state", async ({ page }) => {
  await page.route("**/api/bff/incidents/open**", (route) => route.fulfill({ json: [] }));
  await page.goto("/feedback");
  await expect(page.getByText(/No open incidents/)).toBeVisible();
});
