import { cleanup, render, screen } from "@testing-library/react";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnomalyGroup, ServerGroupBy } from "@/features/anomalies/api/enriched";
import { ServerGroupedList } from "@/features/anomalies/components/GroupedAnomalies";

// ---------------------------------------------------------------------------
// Mocks
//
// useReadingsBounds is unrelated data-fetching plumbing (it only widens the
// jump window to the data horizon) that would otherwise need a
// QueryClientProvider + fetch mock just to reach this render path. Everything
// actually under test here — MonitoringJumpLink's channel param and the
// rendered sensor list — is left real.
// ---------------------------------------------------------------------------

vi.mock("@/features/monitoring/hooks/useReadingsBounds", () => ({
  useReadingsBounds: () => ({ minTimestamp: null, maxTimestamp: null }),
}));

// jsdom has no router, so next/navigation's useSearchParams has nothing to
// read from outside a real app-router tree; stub it the same way the jump
// link's data-fetching hook above is stubbed.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

function Wrapper({ children }: { children: ReactNode }) {
  return <NuqsTestingAdapter>{children}</NuqsTestingAdapter>;
}

function renderGroups(groups: readonly AnomalyGroup[], serverGroupBy: ServerGroupBy) {
  return render(
    <ServerGroupedList
      groups={groups}
      groupsPageIndex={0}
      onGroupsPageChange={() => {}}
      serverGroupBy={serverGroupBy}
      isRefetching={false}
    />,
    { wrapper: Wrapper },
  );
}

/** Pull the `channel=` param off the single jump link's href. */
function jumpChannels(): string[] {
  const link = screen.getByRole("link", { name: /open .* in monitoring/i });
  const href = link.getAttribute("href") ?? "";
  const channel = new URL(href, "http://localhost").searchParams.get("channel");
  return channel ? channel.split(",") : [];
}

afterEach(() => {
  cleanup();
});

describe("ServerGroupedList", () => {
  it("jumps with every affected sensor the row displays, in sensor group-by mode", () => {
    const groups: AnomalyGroup[] = [
      {
        key: "temperature",
        count: 1,
        top_score: 0.91,
        sample_ids: ["2026-01-01T00:00:00Z"],
        samples: [
          {
            timestamp: "2026-01-01T00:00:00Z",
            anomaly_score: 0.91,
            affected_sensors: ["temperature", "humidity"],
            reasoning: "co-moving spike",
            values: {},
          },
        ],
      },
    ];

    renderGroups(groups, "sensor");

    expect(screen.getByTestId("grouped-anomaly-sensors")).toHaveTextContent(
      "temperature, humidity",
    );
    expect(jumpChannels().sort()).toEqual(["humidity", "temperature"]);
  });

  it("falls back to the group's own sensor for both the row and the jump when affected_sensors is empty", () => {
    const groups: AnomalyGroup[] = [
      {
        key: "co2",
        count: 1,
        top_score: 0.5,
        sample_ids: ["2026-01-02T00:00:00Z"],
        samples: [
          {
            timestamp: "2026-01-02T00:00:00Z",
            anomaly_score: 0.5,
            affected_sensors: [],
            reasoning: null,
            values: {},
          },
        ],
      },
    ];

    renderGroups(groups, "sensor");

    expect(screen.getByTestId("grouped-anomaly-sensors")).toHaveTextContent("co2");
    expect(jumpChannels()).toEqual(["co2"]);
  });

  it("leaves non-sensor group-by modes unchanged: row and jump both carry every affected sensor", () => {
    const groups: AnomalyGroup[] = [
      {
        key: "stuck",
        count: 1,
        top_score: 0.8,
        sample_ids: ["2026-01-03T00:00:00Z"],
        samples: [
          {
            timestamp: "2026-01-03T00:00:00Z",
            anomaly_score: 0.8,
            affected_sensors: ["temperature", "co2"],
            reasoning: "stuck sensor",
            values: {},
          },
        ],
      },
    ];

    renderGroups(groups, "type");

    expect(screen.getByTestId("grouped-anomaly-sensors")).toHaveTextContent("temperature, co2");
    expect(jumpChannels().sort()).toEqual(["co2", "temperature"]);
  });

  it("drops the channel param entirely for a non-sensor group-by sample with no affected sensors", () => {
    const groups: AnomalyGroup[] = [
      {
        key: "stuck",
        count: 1,
        top_score: 0.3,
        sample_ids: ["2026-01-04T00:00:00Z"],
        samples: [
          {
            timestamp: "2026-01-04T00:00:00Z",
            anomaly_score: 0.3,
            affected_sensors: [],
            reasoning: null,
            values: {},
          },
        ],
      },
    ];

    renderGroups(groups, "type");

    expect(screen.getByTestId("grouped-anomaly-sensors")).toHaveTextContent("—");
    const link = screen.getByRole("link", { name: /open .* in monitoring/i });
    const href = link.getAttribute("href") ?? "";
    expect(new URL(href, "http://localhost").searchParams.has("channel")).toBe(false);
  });
});
