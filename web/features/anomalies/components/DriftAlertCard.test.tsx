import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  DriftAlertCard,
  isDriftWatchEpisode,
} from "@/features/anomalies/components/DriftAlertCard";

afterEach(cleanup);

const HREF = "/drift-watch?tenant=t1&gh=gh_A";
const episode = { channel: "temperature", start_ts: "2026-11-20T04:00:00Z" };

describe("DriftAlertCard", () => {
  it("renders channel, advisory severity and first-detected time", () => {
    render(<DriftAlertCard episode={episode} href={HREF} />);
    expect(screen.getByTestId("drift-alert-card").textContent).toContain("temperature");
    expect(screen.getByTestId("drift-alert-card").textContent).toContain("2026-11-20 04:00");
    const chip = screen.getByTestId("drift-alert-severity").firstElementChild;
    expect(chip?.getAttribute("data-severity")).toBe("warning");
    expect(screen.getByTestId("drift-alert-severity").textContent).toBe("Advisory");
  });

  it("does not name one probe as the faulty one", () => {
    render(<DriftAlertCard episode={episode} href={HREF} />);
    const text = screen.getByTestId("drift-alert-text").textContent ?? "";
    expect(text).toContain("One of them may have drifted");
    expect(text).toContain("cannot tell which");
    expect(text).not.toMatch(
      /probe (is|has) (faulty|drifted)|temperature (probe|sensor) (is|has)/i,
    );
  });

  it("links to the Drift Watch page with the scope", () => {
    render(<DriftAlertCard episode={episode} href={HREF} />);
    expect(screen.getByTestId("drift-alert-link").getAttribute("href")).toBe(HREF);
  });

  it("only drift_watch rows get the card; other detectors are unchanged", () => {
    expect(isDriftWatchEpisode({ detector: "drift_watch" })).toBe(true);
    expect(isDriftWatchEpisode({ detector: "statistical" })).toBe(false);
    expect(isDriftWatchEpisode({ detector: "stuck" })).toBe(false);
  });
});
