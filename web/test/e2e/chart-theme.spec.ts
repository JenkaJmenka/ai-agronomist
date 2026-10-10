import { expect, type Page, test } from "@playwright/test";

/**
 * Reported 2026-09-14: chart axis labels stayed black in dark mode, where they
 * measure 1.12:1 against the background — unreadable.
 *
 * uPlot paints axes to a canvas and cannot see CSS custom properties, so this
 * asserts the contract directly: the color rendered onto the canvas equals the
 * --text-muted token for the theme that is actually active.
 */

const ROWS = Array.from({ length: 240 }, (_, i) => {
  const t = new Date(Date.UTC(2026, 6, 18, 0, 0, 0) + i * 5 * 60_000);
  return {
    timestamp: t.toISOString(),
    temperature: 22 + 3 * Math.sin(i / 20),
    humidity: 60 + 8 * Math.cos(i / 25),
    co2: 400 + 40 * Math.sin(i / 15),
    light: Math.max(0, 700 * Math.sin((i / 240) * Math.PI)),
    anomaly_score: 0.01,
  };
});

async function mockBff(page: Page): Promise<void> {
  // Playwright matches the most recently registered route first, so the
  // catch-all is registered before the specific handlers that must win.
  await page.route("**/api/bff/**", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/bff/live/**", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/bff/readings/**", (route) =>
    route.fulfill({
      json: {
        rows: ROWS,
        count: ROWS.length,
        min_timestamp: ROWS[0].timestamp,
        max_timestamp: ROWS[ROWS.length - 1].timestamp,
      },
    }),
  );
}

/**
 * The chart canvas is transparent except where uPlot paints, so fully opaque
 * pixels in the x-axis strip are glyph interiors — their most common RGB is
 * the axis text color. Returned alongside the token it should match.
 */
async function axisTextVsToken(page: Page): Promise<{ rendered: string; token: string }> {
  return page.evaluate(() => {
    function toHex(r: number, g: number, b: number): string {
      return `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
    }

    const canvas = document.querySelector(".uplot canvas") as HTMLCanvasElement | null;
    if (!canvas) throw new Error("chart canvas not found");
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("no 2d context");

    const stripHeight = Math.round(44 * window.devicePixelRatio);
    const { data } = ctx.getImageData(0, canvas.height - stripHeight, canvas.width, stripHeight);

    const counts = new Map<string, number>();
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 250) continue; // skip antialiased edges
      const hex = toHex(data[i], data[i + 1], data[i + 2]);
      counts.set(hex, (counts.get(hex) ?? 0) + 1);
    }

    let best = 0;
    let rendered = "none";
    for (const [hex, n] of counts) {
      if (n > best) {
        best = n;
        rendered = hex;
      }
    }

    const token = getComputedStyle(document.documentElement)
      .getPropertyValue("--text-muted")
      .trim()
      .toLowerCase();
    return { rendered, token };
  });
}

test("chart axis text matches the active theme token", async ({ page }) => {
  await mockBff(page);
  await page.goto("/monitoring");
  await expect(page.getByTestId("uplot-chart")).toBeVisible();
  await page.waitForTimeout(500);

  const light = await axisTextVsToken(page);
  expect(light.rendered).toBe(light.token);

  // Flipping the theme must repaint the existing chart, not just new ones.
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.waitForTimeout(500);

  const dark = await axisTextVsToken(page);
  expect(dark.rendered).toBe(dark.token);
  // The whole point: the two themes must not render the same ink.
  expect(dark.rendered).not.toBe(light.rendered);
});
