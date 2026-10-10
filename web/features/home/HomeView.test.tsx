import { cleanup, render } from "@testing-library/react";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_HOME_LAYOUT_PREFS } from "@/features/app-configuration/lib/homeLayoutPrefs";
import { HomeView } from "@/features/home/HomeView";
import type { SensorRow } from "@/features/monitoring/api/readings";

/**
 * The rows the chart is actually handed, per render — the regression this file
 * guards (see `docs/handoff/web_deferred_value_mitigation.md`):
 *
 * - outside live mode the chart must get the raw rows, never a deferred copy;
 * - the `[]` → populated transition must never be deferred, in either mode,
 *   because React keeps rendering `[]` while the populated commit waits at low
 *   priority and can starve for seconds under CPU contention;
 * - a live tick *is* still deferred — that smoothing is why the hook exists.
 */
const chartRowsPerRender: SensorRow[][] = [];

/** Mutable state behind the mocked data hooks; read at render time. */
const readings = {
  rows: [] as SensorRow[],
  isLoading: true,
};
const liveReadings = {
  rows: [] as SensorRow[],
};

vi.mock("@/features/monitoring/hooks/useReadings", () => ({
  useReadings: () => ({
    rows: readings.rows,
    isLoading: readings.isLoading,
    isRefetching: false,
    isError: false,
    error: null,
    downsampled: false,
    originalCount: readings.rows.length,
    windowLabel: "last 24 h",
    from: null,
    to: null,
  }),
}));

vi.mock("@/features/monitoring/hooks/useLiveReadings", () => ({
  useLiveReadings: () => ({
    rows: liveReadings.rows,
    anomalies: [],
    transport: "sse",
    stale: false,
    sourceStatuses: [],
  }),
}));

vi.mock("@/features/monitoring/hooks/useSetpoints", () => ({
  useSetpoints: () => ({
    setpoints: {},
    updateSetpoint: vi.fn(),
    resetToDefaults: vi.fn(),
  }),
}));

vi.mock("@/features/anomalies/hooks/useAnomalyCount", () => ({
  useAnomalyCount: () => ({ count: 0, isLoading: false, isError: false, error: null }),
}));

vi.mock("@/features/anomalies/hooks/useFlaggedWindow", () => ({
  useFlaggedWindow: () => ({
    anomalies: [],
    isLoading: false,
    isRefetching: false,
    isError: false,
    error: null,
  }),
}));

vi.mock("@/features/app-configuration/hooks/useHomeLayoutPrefs", () => ({
  useHomeLayoutPrefs: () => ({
    prefs: {
      ...DEFAULT_HOME_LAYOUT_PREFS,
      showStatusBar: false,
      showTiles: false,
      showAnomalyList: false,
    },
    setPrefs: vi.fn(),
    resetPrefs: vi.fn(),
    hydrated: true,
  }),
}));

// Period/layout controls pull in their own query hooks; the chart is the probe.
vi.mock("@/features/home/components/HomeControls", () => ({
  HomeControls: () => null,
}));

vi.mock("@/features/monitoring/components/SensorTimeSeriesChart", () => ({
  SensorTimeSeriesChart: ({ rows }: { readonly rows: SensorRow[] }) => {
    chartRowsPerRender.push(rows);
    return <div data-testid="chart-probe" />;
  },
}));

const FIRST_BATCH: SensorRow[] = [
  { timestamp: "2026-06-01T00:00:00+00:00", temperature: 21, humidity: 60 },
];
const SECOND_BATCH: SensorRow[] = [
  ...FIRST_BATCH,
  { timestamp: "2026-06-01T00:00:01+00:00", temperature: 22, humidity: 61 },
];

// from/to in the URL keep HomeBody's default-period effect from writing a range.
const WINDOW_PARAMS = "from=2026-05-31T00:00:00.000Z&to=2026-06-02T00:00:00.000Z";

function renderHome(live: boolean) {
  function wrapper({ children }: { readonly children: ReactNode }) {
    return (
      <NuqsTestingAdapter searchParams={`?live=${live}&${WINDOW_PARAMS}`}>
        {children}
      </NuqsTestingAdapter>
    );
  }
  const view = render(<HomeView />, { wrapper });
  return {
    /** Re-renders after the mocked hook state has been mutated. */
    commit(): void {
      view.rerender(<HomeView />);
    },
    /** Chart row lists rendered since the last call, oldest first. */
    drain(): SensorRow[][] {
      return chartRowsPerRender.splice(0, chartRowsPerRender.length);
    },
  };
}

function arriveWithFirstBatch(): void {
  readings.rows = FIRST_BATCH;
  readings.isLoading = false;
}

describe("HomeView — chart row source", () => {
  beforeEach(() => {
    chartRowsPerRender.length = 0;
    readings.rows = [];
    readings.isLoading = true;
    liveReadings.rows = [];
  });
  afterEach(cleanup);

  it("paints the chart with the arriving rows on the first commit when live is off", () => {
    const view = renderHome(false);
    expect(view.drain()).toEqual([]); // still loading — no chart yet

    arriveWithFirstBatch();
    view.commit();

    const rendered = view.drain();
    expect(rendered.at(0)).toHaveLength(FIRST_BATCH.length);
    for (const rows of rendered) expect(rows).toHaveLength(FIRST_BATCH.length);
  });

  it("paints the chart with the arriving rows on the first commit when live is on", () => {
    const view = renderHome(true);
    view.drain();

    arriveWithFirstBatch();
    view.commit();

    // The starvation bug: an empty first commit leaves the operator with no
    // chart until React gets around to the low-priority render.
    expect(view.drain().at(0)).toHaveLength(FIRST_BATCH.length);
  });

  it("still defers a live tick by one render once rows are on screen", () => {
    const view = renderHome(true);
    arriveWithFirstBatch();
    view.commit();
    view.drain();

    liveReadings.rows = SECOND_BATCH;
    view.commit();

    // Smoothing: the urgent render keeps the rows already on screen and the
    // low-priority render catches up to the tick.
    const rendered = view.drain();
    expect(rendered.at(0)).toHaveLength(FIRST_BATCH.length);
    expect(rendered.at(-1)).toHaveLength(SECOND_BATCH.length);
  });

  it("does not defer a historical window change when live is off", () => {
    const view = renderHome(false);
    arriveWithFirstBatch();
    view.commit();
    view.drain();

    readings.rows = SECOND_BATCH;
    view.commit();

    for (const rows of view.drain()) expect(rows).toHaveLength(SECOND_BATCH.length);
  });
});
