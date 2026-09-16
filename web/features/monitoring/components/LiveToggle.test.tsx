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
// Resume-before-bounds ordering (fixes the toggle-on/toggle-off race: a fast
// off-click must never pause a scope that hasn't been resumed yet at the
// server, which would leave the scope running while the UI shows Live off).
// ---------------------------------------------------------------------------

describe("LiveToggle — resume ordering on toggle-on", () => {
  it("awaits resumeLiveScope before starting the bounds fetch", async () => {
    let resolveResume: () => void = () => {};
    const resumeDeferred = new Promise<void>((resolve) => {
      resolveResume = resolve;
    });
    resumeLiveScopeMock.mockReturnValue(resumeDeferred);
    fetchReadingsBoundsMock.mockResolvedValue({ min_timestamp: null, max_timestamp: null });

    render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    await waitFor(() => expect(resumeLiveScopeMock).toHaveBeenCalledTimes(1));
    expect(resumeLiveScopeMock).toHaveBeenCalledWith({ tenant: "tenant_1", gh: "gh_A" });

    // While the resume request is still in flight, the bounds fetch (and
    // therefore any later window snap) must not have started yet.
    expect(fetchReadingsBoundsMock).not.toHaveBeenCalled();

    resolveResume();
    await waitFor(() => expect(fetchReadingsBoundsMock).toHaveBeenCalledTimes(1));
  });

  it("still starts the bounds fetch when resumeLiveScope rejects (tolerant)", async () => {
    resumeLiveScopeMock.mockRejectedValue(new Error("network error"));
    fetchReadingsBoundsMock.mockResolvedValue({ min_timestamp: null, max_timestamp: null });

    render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    await waitFor(() => expect(fetchReadingsBoundsMock).toHaveBeenCalledTimes(1));
  });
});
