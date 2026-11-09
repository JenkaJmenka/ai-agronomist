import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useChannelVisibility } from "@/features/monitoring/hooks/useChannelVisibility";

// ---------------------------------------------------------------------------
// Mocks
//
// The hook reads/writes `channel` via nuqs's useQueryState directly (not
// through a shared hook), so it is stubbed the same way
// LiveToggle.test.tsx stubs it rather than pulling in a real nuqs adapter.
// The hook only ever calls the setter (to drop sticky jump focus); it never
// reads the tuple's first value, so the stub's value side is unused.
// ---------------------------------------------------------------------------

const setChannelParamMock = vi.fn().mockResolvedValue(new URLSearchParams());

vi.mock("nuqs", () => ({
  useQueryState: () => [null, setChannelParamMock],
  parseAsString: {},
}));

const CHANNELS = ["temperature", "humidity", "co2", "light"];
// A fourth channel ("light") that is never part of the jump or any toggle in
// these tests. Without it, "jump focus + one toggled channel" happens to
// equal "all page channels" for this fixture, so a test asserting the
// correct 3-channel result would also pass against the pre-fix code, which
// falls back to showing every channel. Keeping a channel that never appears
// in any expected Set is what makes the two outcomes distinguishable.

function renderChannelVisibility(channelParam: string | null) {
  return renderHook(
    (props: { channelParam: string | null }) =>
      useChannelVisibility({
        discoveredNow: CHANNELS,
        channelParam: props.channelParam,
        tenant: "tenant_1",
        gh: "gh_A",
      }),
    { initialProps: { channelParam } },
  );
}

describe("useChannelVisibility", () => {
  it("pre-selects exactly the matched channels on a multi-sensor jump", () => {
    const { result } = renderChannelVisibility("temperature,humidity");

    expect(result.current.jumpFocus).toEqual(["temperature", "humidity"]);
    expect(result.current.visible).toEqual(new Set(["temperature", "humidity"]));
  });

  it("toggling one more channel after a jump adds just that channel, not all channels", () => {
    const { result, rerender } = renderChannelVisibility("temperature,humidity");

    act(() => {
      result.current.toggleChannel("co2");
    });
    // toggleChannel's setChannelParam(null) call lands synchronously in the
    // real app (nuqs updates the URL, which this page's own useQueryState
    // subscription reflects straight back as a re-render), so the user sees
    // this post-round-trip state immediately after the click — reproduce it.
    rerender({ channelParam: null });

    expect(result.current.visible).toEqual(new Set(["temperature", "humidity", "co2"]));
  });

  it("toggling a channel off removes only it", () => {
    const { result } = renderChannelVisibility("temperature,humidity");

    act(() => {
      result.current.toggleChannel("temperature");
    });

    expect(result.current.visible).toEqual(new Set(["humidity"]));
  });

  it("does not reset the manual selection when a toggle-off round-trips through a null channelParam", () => {
    const { result, rerender } = renderChannelVisibility("temperature,humidity");

    act(() => {
      result.current.toggleChannel("humidity");
    });
    expect(setChannelParamMock).toHaveBeenCalledWith(null);

    rerender({ channelParam: null });

    expect(result.current.visible).toEqual(new Set(["temperature"]));
  });

  it("re-applies pre-select on a new, different, non-null channelParam", () => {
    const { result, rerender } = renderChannelVisibility("temperature,humidity");

    act(() => {
      result.current.toggleChannel("co2");
    });
    rerender({ channelParam: null });
    expect(result.current.visible).toEqual(new Set(["temperature", "humidity", "co2"]));

    // A fresh jump to a different sensor should override the stale manual
    // selection from the previous jump, not merge with it.
    rerender({ channelParam: "co2" });

    expect(result.current.jumpFocus).toEqual(["co2"]);
    expect(result.current.visible).toEqual(new Set(["co2"]));
  });

  it("shows all channels when channelParam matches nothing on this page", () => {
    const { result } = renderChannelVisibility("not_a_real_sensor");

    expect(result.current.jumpFocus).toEqual([]);
    expect(result.current.visible).toEqual(new Set(CHANNELS));
  });

  it("applies both toggles when two calls happen in the same batch (no stale-closure overwrite)", () => {
    const { result } = renderChannelVisibility("temperature,humidity");

    act(() => {
      result.current.toggleChannel("co2");
      result.current.toggleChannel("humidity");
    });

    // co2 added, humidity removed, temperature untouched — each toggle must
    // see the other's effect rather than both reading the same stale set.
    expect(result.current.visible).toEqual(new Set(["temperature", "co2"]));
  });
});
