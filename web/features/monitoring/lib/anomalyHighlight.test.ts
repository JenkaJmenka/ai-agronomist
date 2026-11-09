import { describe, expect, it } from "vitest";

import {
  matchChannelParam,
  shouldPaintAnomalyHighlight,
} from "@/features/monitoring/lib/anomalyHighlight";

describe("matchChannelParam", () => {
  it("matches comma-separated jump sensors onto page channels", () => {
    expect(matchChannelParam(["temperature", "humidity", "co2"], "temperature,humidity")).toEqual([
      "temperature",
      "humidity",
    ]);
  });

  it("returns empty when jump sensors are absent on this page", () => {
    expect(matchChannelParam(["c2h4", "gas_co2"], "temperature,humidity")).toEqual([]);
  });

  it("matches a raw aliased detector column onto its canonical page channel", () => {
    expect(
      matchChannelParam(["temperature", "humidity", "co2"], "measured_greenhouse_temp"),
    ).toEqual(["temperature"]);
  });

  it("pre-selects both a raw aliased name and a canonical name in the same jump", () => {
    expect(matchChannelParam(["temperature", "co2"], "measured_greenhouse_temp,co2")).toEqual([
      "temperature",
      "co2",
    ]);
  });

  it("matches measured_humidity_deficit onto the measured_hd canonical channel", () => {
    expect(matchChannelParam(["measured_hd", "co2"], "measured_humidity_deficit")).toEqual([
      "measured_hd",
    ]);
  });
});

describe("shouldPaintAnomalyHighlight", () => {
  const page = ["temperature", "humidity", "co2"];

  it("allows score highlight when there is no jump channel focus", () => {
    expect(
      shouldPaintAnomalyHighlight({
        channelParam: null,
        pageChannels: page,
        visible: new Set(["co2"]),
      }),
    ).toBe(true);
  });

  it("suppresses highlight when jump sensors are not on this page (Gas)", () => {
    expect(
      shouldPaintAnomalyHighlight({
        channelParam: "temperature,humidity",
        pageChannels: ["c2h4", "nox"],
        visible: new Set(["c2h4", "nox"]),
      }),
    ).toBe(false);
  });

  it("suppresses highlight when jump sensors exist but are all toggled off", () => {
    expect(
      shouldPaintAnomalyHighlight({
        channelParam: "temperature,humidity",
        pageChannels: page,
        visible: new Set(["co2"]),
      }),
    ).toBe(false);
  });

  it("allows highlight while at least one jump sensor is active", () => {
    expect(
      shouldPaintAnomalyHighlight({
        channelParam: "temperature,humidity",
        pageChannels: page,
        visible: new Set(["humidity"]),
      }),
    ).toBe(true);
  });
});
