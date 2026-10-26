import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SensorTile } from "@/features/monitoring/components/SensorTile";
import { CHANNEL_META } from "@/features/monitoring/lib/channelMeta";

// The shared vitest setup has no RTL auto-cleanup; without this, tiles from
// prior tests accumulate and getByTestId finds duplicates.
afterEach(cleanup);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface TileOpts {
  channel?: string;
  value?: number | null;
  status?: "ok" | "warning" | "critical" | "no_range";
  anomalyCount?: number;
  isLive?: boolean;
  isStale?: boolean;
  setpointHint?: string | null;
  relatedAnomalyChannel?: string | null;
}

function renderTile({
  channel = "temperature",
  value = 22.5,
  status = "ok",
  anomalyCount = 0,
  isLive = false,
  isStale = false,
  setpointHint,
  relatedAnomalyChannel,
}: TileOpts = {}) {
  return render(
    <SensorTile
      channel={channel}
      value={value}
      meta={CHANNEL_META[channel]}
      status={status}
      anomalyCount={anomalyCount}
      isLive={isLive}
      isStale={isStale}
      setpointHint={setpointHint}
      relatedAnomalyChannel={relatedAnomalyChannel}
    />,
  );
}

// ---------------------------------------------------------------------------
// data-testid
// ---------------------------------------------------------------------------

describe("SensorTile — testid", () => {
  it("sets data-testid to sensor-tile-{channel}", () => {
    renderTile({ channel: "temperature" });
    expect(screen.getByTestId("sensor-tile-temperature")).toBeInTheDocument();
  });

  it("uses the channel name in data-testid for vpd", () => {
    renderTile({ channel: "vpd", value: 0.9 });
    expect(screen.getByTestId("sensor-tile-vpd")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Value rendering
// ---------------------------------------------------------------------------

describe("SensorTile — value rendering", () => {
  it("shows the formatted value with the correct unit", () => {
    renderTile({ channel: "temperature", value: 22.5, status: "ok" });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile).toHaveTextContent("22.5");
    expect(tile).toHaveTextContent("°C");
  });

  it("rounds value to 0 decimals for co2", () => {
    renderTile({ channel: "co2", value: 420.6 });
    const tile = screen.getByTestId("sensor-tile-co2");
    expect(tile).toHaveTextContent("421");
  });

  it("shows em-dash when value is null", () => {
    renderTile({ channel: "temperature", value: null });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile).toHaveTextContent("—");
  });

  it("shows the channel label from CHANNEL_META", () => {
    renderTile({ channel: "humidity", value: 60 });
    expect(screen.getByTestId("sensor-tile-humidity")).toHaveTextContent("Humidity");
  });

  it("falls back to the raw channel name when not in CHANNEL_META", () => {
    render(
      <SensorTile
        channel="mystery_sensor"
        value={42}
        meta={undefined}
        status="ok"
        isLive={false}
        isStale={false}
      />,
    );
    expect(screen.getByTestId("sensor-tile-mystery_sensor")).toHaveTextContent("mystery_sensor");
  });
});

// ---------------------------------------------------------------------------
// Status-based styling — setpoint-only (no anomaly)
// ---------------------------------------------------------------------------

describe("SensorTile — setpoint-only status styles (no anomaly)", () => {
  it("applies ok border class", () => {
    renderTile({ status: "ok", anomalyCount: 0 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("border-l-sev-normal");
  });

  it("applies amber (warning) border class for warning setpoint excursion", () => {
    renderTile({ status: "warning", anomalyCount: 0 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("border-l-sev-warning");
    expect(tile.className).not.toContain("border-l-sev-critical");
  });

  it("applies amber (warning) border class for critical setpoint excursion — NOT red", () => {
    renderTile({ status: "critical", anomalyCount: 0 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    // Critical setpoint without anomaly gets amber treatment, not red
    expect(tile.className).toContain("border-l-sev-warning");
    expect(tile.className).not.toContain("border-l-sev-critical");
  });

  it("applies amber background for setpoint excursion (warning or critical)", () => {
    renderTile({ status: "critical", anomalyCount: 0 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("bg-sev-warning");
    expect(tile.className).not.toContain("bg-sev-critical");
  });
});

describe("SensorTile — no range is neutral, never green", () => {
  it("uses a neutral border for no_range", () => {
    renderTile({ channel: "heating_temp", value: 45, status: "no_range" });
    const tile = screen.getByTestId("sensor-tile-heating_temp");
    expect(tile.className).toContain("border-l-border");
    expect(tile.className).not.toContain("border-l-sev-normal");
    expect(tile).toHaveAttribute("data-setpoint-state", "no_range");
  });
});

// ---------------------------------------------------------------------------
// Anomaly dominant — overrides setpoint visuals
// ---------------------------------------------------------------------------

describe("SensorTile — anomaly is the dominant visual signal", () => {
  it("shows red border for an in-range tile the detector flagged", () => {
    renderTile({ status: "ok", anomalyCount: 3 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("border-l-sev-critical");
    expect(tile.className).not.toContain("border-l-sev-normal");
  });

  it("shows red bg for an in-range tile the detector flagged", () => {
    renderTile({ status: "ok", anomalyCount: 2 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("bg-sev-critical");
    expect(tile.className).not.toContain("bg-bg-elevated");
  });

  it("shows red border even when setpoint is critical (anomaly dominates)", () => {
    renderTile({ status: "critical", anomalyCount: 1 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("border-l-sev-critical");
    expect(tile.className).not.toContain("border-l-sev-warning");
  });

  it("shows red border for no_range tile with anomaly", () => {
    renderTile({ status: "no_range", anomalyCount: 1 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("border-l-sev-critical");
    expect(tile.className).not.toContain("border-l-border");
  });
});

// ---------------------------------------------------------------------------
// Anomaly badge
// ---------------------------------------------------------------------------

describe("SensorTile — anomaly badge is independent of setpoint state", () => {
  it("shows no badge when the detector flagged nothing", () => {
    renderTile({ status: "critical", anomalyCount: 0 });
    expect(screen.queryByTestId("sensor-tile-anomaly-temperature")).toBeNull();
  });

  it("shows the badge on an in-range tile the detector flagged", () => {
    renderTile({ status: "ok", anomalyCount: 3 });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(screen.getByTestId("sensor-tile-anomaly-temperature")).toHaveTextContent("3 anomalies");
    expect(tile).toHaveAttribute("data-anomaly-count", "3");
  });

  it("uses the singular for one anomaly", () => {
    renderTile({ status: "no_range", anomalyCount: 1 });
    expect(screen.getByTestId("sensor-tile-anomaly-temperature")).toHaveTextContent(/^1 anomaly$/);
  });
});

// ---------------------------------------------------------------------------
// Setpoint hint
// ---------------------------------------------------------------------------

describe("SensorTile — setpoint direction hint", () => {
  it("shows the setpoint hint for a critical excursion with no anomaly", () => {
    renderTile({
      channel: "humidity",
      value: 29,
      status: "critical",
      anomalyCount: 0,
      setpointHint: "below setpoint 53–77",
    });
    expect(screen.getByTestId("sensor-tile-setpoint-hint-humidity")).toHaveTextContent(
      "below setpoint 53–77",
    );
  });

  it("does not show a setpoint hint when there is an anomaly", () => {
    renderTile({
      channel: "humidity",
      value: 29,
      status: "critical",
      anomalyCount: 2,
      setpointHint: "below setpoint 53–77",
    });
    expect(screen.queryByTestId("sensor-tile-setpoint-hint-humidity")).toBeNull();
  });

  it("does not show a setpoint hint when the value is in range", () => {
    renderTile({
      channel: "humidity",
      value: 65,
      status: "ok",
      anomalyCount: 0,
      setpointHint: null,
    });
    expect(screen.queryByTestId("sensor-tile-setpoint-hint-humidity")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Related-anomaly hint
// ---------------------------------------------------------------------------

describe("SensorTile — related-anomaly hint", () => {
  it("shows the related-anomaly hint for a setpoint excursion with a coupled anomalous channel", () => {
    renderTile({
      channel: "humidity",
      value: 29,
      status: "critical",
      anomalyCount: 0,
      relatedAnomalyChannel: "measured_hd",
    });
    const hint = screen.getByTestId("sensor-tile-related-humidity");
    expect(hint).toHaveTextContent("related to Humidity Deficit anomaly");
    expect(screen.getByTestId("sensor-tile-humidity")).toHaveAttribute(
      "data-related-anomaly",
      "measured_hd",
    );
  });

  it("does not show the related hint when the tile itself has an anomaly", () => {
    renderTile({
      channel: "humidity",
      value: 29,
      status: "critical",
      anomalyCount: 2,
      relatedAnomalyChannel: "measured_hd",
    });
    expect(screen.queryByTestId("sensor-tile-related-humidity")).toBeNull();
    expect(screen.getByTestId("sensor-tile-humidity")).not.toHaveAttribute("data-related-anomaly");
  });

  it("does not show the related hint for an in-range tile", () => {
    renderTile({
      channel: "humidity",
      value: 65,
      status: "ok",
      anomalyCount: 0,
      relatedAnomalyChannel: "measured_hd",
    });
    expect(screen.queryByTestId("sensor-tile-related-humidity")).toBeNull();
  });

  it("does not add data-related-anomaly when no related channel", () => {
    renderTile({
      channel: "humidity",
      value: 29,
      status: "critical",
      anomalyCount: 0,
      relatedAnomalyChannel: null,
    });
    expect(screen.getByTestId("sensor-tile-humidity")).not.toHaveAttribute("data-related-anomaly");
  });
});

// ---------------------------------------------------------------------------
// Live / stale dimming
// ---------------------------------------------------------------------------

describe("SensorTile — opacity based on live/stale state", () => {
  it("dims when not live (historical mode)", () => {
    renderTile({ isLive: false });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("opacity-60");
  });

  it("dims when stream is stale", () => {
    renderTile({ isLive: true, isStale: true });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("opacity-60");
  });

  it("is not dimmed when live and not stale", () => {
    renderTile({ isLive: true, isStale: false });
    const tile = screen.getByTestId("sensor-tile-temperature");
    expect(tile.className).toContain("opacity-100");
    expect(tile.className).not.toContain("opacity-60");
  });
});

// ---------------------------------------------------------------------------
// Provenance hint
// ---------------------------------------------------------------------------

describe("SensorTile — derived provenance", () => {
  it("shows the derived badge for vpd", () => {
    renderTile({ channel: "vpd", value: 0.85 });
    expect(screen.getByTestId("sensor-tile-vpd")).toHaveTextContent("derived");
  });

  it("does not show derived badge for temperature", () => {
    renderTile({ channel: "temperature", value: 22 });
    expect(screen.getByTestId("sensor-tile-temperature")).not.toHaveTextContent("derived");
  });
});
