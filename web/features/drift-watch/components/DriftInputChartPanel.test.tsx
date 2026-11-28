import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import type uPlot from "uplot";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DriftInputChartPanel } from "@/features/drift-watch/components/DriftInputChartPanel";

type ChartOptions = Omit<uPlot.Options, "width" | "height">;
interface Captured {
  readonly options: ChartOptions;
  readonly data: uPlot.AlignedData;
}
let charts: Captured[] = [];

vi.mock("@/components/charts/UPlotChart", () => ({
  UPlotChart: (props: Captured) => {
    charts.push({ options: props.options, data: props.data });
    return null;
  },
}));

vi.mock("@/lib/useScope", () => ({
  useScope: vi.fn(() => ({ tenant: "tenant_1", gh: "gh_A" })),
}));

const point = (hour: number) => ({
  ts: `2026-10-01T${String(hour).padStart(2, "0")}:00:00Z`,
  target: 20 + hour,
  reference: 19 + hour,
  dev: hour / 10,
  s_pos: hour / 5,
  s_neg: 0,
});

function series(over: Record<string, unknown> = {}) {
  return {
    input_id: "in1",
    kind: "paired_probes",
    target_channel: "temperature",
    unit: "°C",
    status: "armed",
    alert_state: "enabled",
    min_shift: 0.3,
    alert_floor: 0.5,
    h: 4,
    sigma: 0.2,
    window_start: "2026-10-01T00:00:00Z",
    window_end: "2026-10-02T00:00:00Z",
    points: [point(1), point(2), point(3)],
    episodes: [
      {
        first_detected_ts: "2026-10-01T04:00:00Z",
        last_confirmed_ts: "2026-10-01T08:00:00Z",
        direction: "+",
        label: null,
      },
      {
        first_detected_ts: "2026-10-01T10:00:00Z",
        last_confirmed_ts: "2026-10-01T12:00:00Z",
        direction: "-",
        label: "confirmed_drift",
      },
      {
        first_detected_ts: "2026-10-01T14:00:00Z",
        last_confirmed_ts: "2026-10-01T16:00:00Z",
        direction: "+",
        label: "false_alarm",
        suppressed_by: "stuck",
      },
    ],
    replay_mismatch: 0,
    reason: null,
    ...over,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

function mount(payload: unknown) {
  fetchMock = vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <NuqsTestingAdapter>
        <DriftInputChartPanel inputId="in1" />
      </NuqsTestingAdapter>
    </QueryClientProvider>,
  );
}

async function open() {
  fireEvent.click(screen.getByTestId("chart-toggle"));
  await waitFor(() => expect(screen.getByTestId("drift-series-panel")).toBeInTheDocument());
}

beforeEach(() => {
  charts = [];
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DriftInputChartPanel", () => {
  it("fetches the series only after the panel is opened", async () => {
    mount(series());
    expect(fetchMock).not.toHaveBeenCalled();
    await open();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/inputs/tenant_1/gh_A/in1/series?");
  });

  it("renders one band per episode with a label-based class and suppressed outline", async () => {
    mount(series());
    await open();
    const bands = await screen.findAllByTestId("episode-band");
    expect(bands.map((b) => b.getAttribute("data-label"))).toEqual([
      "unlabelled",
      "confirmed_drift",
      "false_alarm",
    ]);
    expect(bands[0]?.className).toContain("--sev-warning");
    expect(bands[1]?.className).toContain("--sev-ok");
    expect(bands[2]?.className).toContain("--text-muted");
    expect(bands.map((b) => b.getAttribute("data-suppressed"))).toEqual(["false", "false", "true"]);
  });

  it("draws the alert floor and h as dashed constant lines, plus the probe chart", async () => {
    mount(series());
    await open();
    await screen.findByTestId("chart-probes");
    const [residual, stat] = charts.slice(-3);
    const labels = (c?: Captured) => (c?.options.series ?? []).map((s) => s.label);
    expect(labels(residual)).toEqual([undefined, "Deviation", "+ alert floor", "- alert floor"]);
    expect(residual?.data[2]).toEqual([0.5, 0.5, 0.5]);
    expect(residual?.data[3]).toEqual([-0.5, -0.5, -0.5]);
    expect(residual?.options.series?.[2]?.dash).toBeDefined();
    expect(labels(stat)).toContain("Alert threshold (h)");
    expect(stat?.data[3]).toEqual([4, 4, 4]);
    expect(stat?.options.axes?.[1]?.label).toBe("Drift statistic (σ)");
  });

  it("shows the replay mismatch note when episode starts were not recorded", async () => {
    mount(series({ replay_mismatch: 3 }));
    await open();
    expect(await screen.findByTestId("series-mismatch-note")).toHaveTextContent(
      "The chart is a replay from calibration. It found 3 episode starts that drift watch did not record (for example, before the first upload). Shaded bands show the recorded episodes.",
    );
  });

  it("shows the reason instead of charts when there is no calibration", async () => {
    mount(series({ reason: "No calibration yet.", points: [], episodes: [] }));
    await open();
    expect(await screen.findByTestId("series-reason")).toHaveTextContent("No calibration yet.");
    expect(screen.queryByTestId("chart-residual")).not.toBeInTheDocument();
  });
});
