import { describe, expect, it } from "vitest";

import {
  homeChartVisible,
  orderHomeChannels,
  selectHomeChannels,
} from "@/features/home/lib/homeChannels";

describe("orderHomeChannels", () => {
  it("puts climate channels first in priority order", () => {
    expect(orderHomeChannels(["nox", "humidity", "temperature", "co"])).toEqual([
      "temperature",
      "humidity",
      "co",
      "nox",
    ]);
  });
});

describe("selectHomeChannels", () => {
  it("returns preferred channels that exist", () => {
    expect(
      selectHomeChannels(["temperature", "humidity", "co2", "light"], ["humidity", "co2"]),
    ).toEqual(["humidity", "co2"]);
  });

  it("falls back to ordered discovery when none match", () => {
    expect(selectHomeChannels(["temperature", "humidity"], ["gas_co2"])).toEqual([
      "temperature",
      "humidity",
    ]);
  });
});

describe("homeChartVisible", () => {
  it("defaults to temp/humidity/co2 when present", () => {
    expect([...homeChartVisible(["temperature", "humidity", "co2", "light"], [])].sort()).toEqual([
      "co2",
      "humidity",
      "temperature",
    ]);
  });

  it("uses primary channels when provided", () => {
    expect([...homeChartVisible(["temperature", "light", "co2"], ["light", "co2"])].sort()).toEqual(
      ["co2", "light"],
    );
  });
});
