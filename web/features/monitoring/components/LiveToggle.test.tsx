import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LiveToggle } from "@/features/monitoring/components/LiveToggle";

// ---------------------------------------------------------------------------
// Mocks
//
// LiveToggle reads `live` via nuqs's useQueryState directly (not through a
// shared hook), so it is stubbed here rather than pulling in a real nuqs
// adapter. tenant/gh and the date-range window come from the shared hooks,
// mocked the same way useLiveReadings.test.ts mocks useScope.
// ---------------------------------------------------------------------------

const setLiveMock = vi.fn().mockResolvedValue(new URLSearchParams());
const setRangeMock = vi.fn().mockResolvedValue(new URLSearchParams());

vi.mock("nuqs", () => ({
  useQueryState: () => [false, setLiveMock],
  parseAsBoolean: { withDefault: () => undefined },
}));

vi.mock("@/lib/useDateRange", () => ({
  useDateRange: () => [{ from: null, to: null }, setRangeMock],
}));

vi.mock("@/lib/useScope", () => ({
  useScope: () => ({ tenant: "tenant_1", gh: "gh_A" }),
}));

const resumeLiveScopeMock = vi.fn();
const pauseLiveScopeMock = vi.fn();
vi.mock("@/features/monitoring/api/live", () => ({
  resumeLiveScope: (...args: unknown[]) => resumeLiveScopeMock(...args),
  pauseLiveScope: (...args: unknown[]) => pauseLiveScopeMock(...args),
}));

const fetchReadingsBoundsMock = vi.fn();
vi.mock("@/features/monitoring/api/readings", () => ({
  fetchReadingsBounds: (...args: unknown[]) => fetchReadingsBoundsMock(...args),
}));

beforeEach(() => {
  setLiveMock.mockClear();
  setRangeMock.mockClear();
  resumeLiveScopeMock.mockReset();
  pauseLiveScopeMock.mockReset();
  fetchReadingsBoundsMock.mockReset();
});

afterEach(cleanup);

// ---------------------------------------------------------------------------
// Resume-before-bounds dispatch order (fixes the toggle-on/toggle-off race: a
// fast off-click's pause must never be sent before the resume for the same
// toggle-on, which would leave the scope running while the UI shows Live
// off). The resume request itself is no longer awaited — dispatch order,
// not resolution order, is what the server's asyncio.Lock relies on — so
// the bounds fetch is expected to proceed without waiting for resume to
// settle.
// ---------------------------------------------------------------------------

describe("LiveToggle — resume ordering on toggle-on", () => {
  it("initiates resumeLiveScope before fetchReadingsBounds, without waiting for it to resolve", async () => {
    const callOrder: string[] = [];
    let resolveResume: () => void = () => {};
    const resumeDeferred = new Promise<void>((resolve) => {
      resolveResume = resolve;
    });
    resumeLiveScopeMock.mockImplementation(() => {
      callOrder.push("resume");
      return resumeDeferred;
    });
    fetchReadingsBoundsMock.mockImplementation(async () => {
      callOrder.push("bounds");
      return { min_timestamp: null, max_timestamp: null };
    });

    render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    // The bounds fetch proceeds even while resume's promise is still
    // pending — proving the wait was removed, not just reordered.
    await waitFor(() => expect(fetchReadingsBoundsMock).toHaveBeenCalledTimes(1));

    expect(callOrder).toEqual(["resume", "bounds"]);
    expect(resumeLiveScopeMock).toHaveBeenCalledWith({ tenant: "tenant_1", gh: "gh_A" });

    resolveResume();
  });

  it("still starts the bounds fetch when resumeLiveScope rejects (tolerant)", async () => {
    resumeLiveScopeMock.mockRejectedValue(new Error("network error"));
    fetchReadingsBoundsMock.mockResolvedValue({ min_timestamp: null, max_timestamp: null });

    render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    await waitFor(() => expect(fetchReadingsBoundsMock).toHaveBeenCalledTimes(1));
  });
});
