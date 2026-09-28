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

const useScopeMock = vi.fn().mockReturnValue({ tenant: "tenant_1", gh: "gh_A" });
vi.mock("@/lib/useScope", () => ({
  useScope: () => useScopeMock(),
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
  useScopeMock.mockReturnValue({ tenant: "tenant_1", gh: "gh_A" });
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

// ---------------------------------------------------------------------------
// scope_status reaction — an unconfigured scope will never stream, so the
// toggle must not keep claiming Live. A missing scope_status (deploy-skew:
// older API) must never revert the toggle.
// ---------------------------------------------------------------------------

describe("LiveToggle — reacts to resume's scope_status", () => {
  it("reverts the toggle and shows a message when scope_status is not_configured", async () => {
    resumeLiveScopeMock.mockResolvedValue({
      resumed: false,
      scope_status: "not_configured",
      tenant_id: "tenant_1",
      greenhouse_id: "gh_A",
    });
    fetchReadingsBoundsMock.mockResolvedValue({ min_timestamp: null, max_timestamp: null });

    render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    await waitFor(() => expect(setLiveMock).toHaveBeenCalledWith(false));
    expect(await screen.findByTestId("live-not-configured")).toHaveTextContent(
      "No live data source is configured for this greenhouse.",
    );
  });

  it("leaves the toggle alone when scope_status is absent (older API)", async () => {
    resumeLiveScopeMock.mockResolvedValue({
      resumed: true,
      tenant_id: "tenant_1",
      greenhouse_id: "gh_A",
    });
    fetchReadingsBoundsMock.mockResolvedValue({ min_timestamp: null, max_timestamp: null });

    render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    await waitFor(() => expect(fetchReadingsBoundsMock).toHaveBeenCalledTimes(1));

    expect(setLiveMock).not.toHaveBeenCalledWith(false);
    expect(screen.queryByTestId("live-not-configured")).not.toBeInTheDocument();
  });

  it("leaves the toggle alone when scope_status is started", async () => {
    resumeLiveScopeMock.mockResolvedValue({
      resumed: true,
      scope_status: "started",
      tenant_id: "tenant_1",
      greenhouse_id: "gh_A",
    });
    fetchReadingsBoundsMock.mockResolvedValue({ min_timestamp: null, max_timestamp: null });

    render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    await waitFor(() => expect(fetchReadingsBoundsMock).toHaveBeenCalledTimes(1));

    expect(setLiveMock).not.toHaveBeenCalledWith(false);
    expect(screen.queryByTestId("live-not-configured")).not.toBeInTheDocument();
  });

  it("leaves the toggle ON with no alert when scope_status is unmanaged (runtime unavailable)", async () => {
    // "unmanaged" means the API couldn't determine the verdict — not that
    // the scope is known-unconfigured. Reacting to it would regress the
    // tolerant behavior for an indeterminate response.
    resumeLiveScopeMock.mockResolvedValue({
      resumed: false,
      scope_status: "unmanaged",
      tenant_id: "tenant_1",
      greenhouse_id: "gh_A",
    });
    fetchReadingsBoundsMock.mockResolvedValue({ min_timestamp: null, max_timestamp: null });

    render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    await waitFor(() => expect(fetchReadingsBoundsMock).toHaveBeenCalledTimes(1));

    expect(setLiveMock).not.toHaveBeenCalledWith(false);
    expect(screen.queryByTestId("live-not-configured")).not.toBeInTheDocument();
  });

  it("clears a stale not_configured message when the scope changes, without a click", async () => {
    resumeLiveScopeMock.mockResolvedValue({
      resumed: false,
      scope_status: "not_configured",
      tenant_id: "tenant_1",
      greenhouse_id: "gh_A",
    });
    fetchReadingsBoundsMock.mockResolvedValue({ min_timestamp: null, max_timestamp: null });

    const { rerender } = render(<LiveToggle pageId="monitoring" />);
    fireEvent.click(screen.getByTestId("live-toggle"));

    expect(await screen.findByTestId("live-not-configured")).toBeInTheDocument();

    // Operator switches to a different, configured greenhouse — GreenhouseSelector
    // writes the URL via its own useQueryState, LiveToggle re-renders with a
    // new scope but does not remount, so its local state would otherwise
    // survive the switch.
    useScopeMock.mockReturnValue({ tenant: "tenant_1", gh: "gh_B" });
    rerender(<LiveToggle pageId="monitoring" />);

    await waitFor(() =>
      expect(screen.queryByTestId("live-not-configured")).not.toBeInTheDocument(),
    );
  });
});
