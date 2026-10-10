/** @vitest-environment jsdom */

import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useLiveDeferred } from "@/lib/useLiveDeferred";

interface Row {
  readonly timestamp: string;
}

const EMPTY: Row[] = [];
const FIRST_BATCH: Row[] = [{ timestamp: "2026-06-01T00:00:00Z" }];
const SECOND_BATCH: Row[] = [...FIRST_BATCH, { timestamp: "2026-06-01T00:00:01Z" }];

interface HookProps {
  readonly rows: Row[];
  readonly live: boolean;
}

/**
 * Renders the hook and records the value returned by *every* render, including
 * React's low-priority re-render. The recorded sequence is what distinguishes
 * "deferred" (a render returns the previous rows) from "immediate".
 */
function renderRecording(initialProps: HookProps) {
  const rendered: Row[][] = [];
  const view = renderHook(
    ({ rows, live }: HookProps) => {
      const value = useLiveDeferred(rows, live);
      rendered.push(value);
      return value;
    },
    { initialProps },
  );
  return {
    rendered,
    /** Values rendered since the last call, oldest first. */
    drain(): Row[][] {
      return rendered.splice(0, rendered.length);
    },
    rerender: view.rerender,
  };
}

describe("useLiveDeferred", () => {
  it("returns the rows array identity-equal when live is off", () => {
    const { result } = renderHook(() => useLiveDeferred(FIRST_BATCH, false));
    expect(result.current).toBe(FIRST_BATCH);
  });

  it("returns the rows array identity-equal on the first render when live is on", () => {
    const { result } = renderHook(() => useLiveDeferred(FIRST_BATCH, true));
    expect(result.current).toBe(FIRST_BATCH);
  });

  it("never renders the stale rows on an update when live is off", () => {
    const view = renderRecording({ rows: FIRST_BATCH, live: false });
    view.drain();

    view.rerender({ rows: SECOND_BATCH, live: false });

    // React still schedules the low-priority catch-up render, so the render
    // count is not the signal — every rendered value being the fresh rows is.
    for (const value of view.drain()) expect(value).toBe(SECOND_BATCH);
  });

  it("defers a live tick by rendering the previous rows first", () => {
    const view = renderRecording({ rows: FIRST_BATCH, live: true });
    view.drain();

    view.rerender({ rows: SECOND_BATCH, live: true });

    // Smoothing: the urgent render keeps the previous rows, the low-priority
    // render catches up. Both commits are flushed by rerender's act().
    const sequence = view.drain();
    expect(sequence.at(0)).toBe(FIRST_BATCH);
    expect(sequence.at(-1)).toBe(SECOND_BATCH);
  });

  it("does not defer the empty to populated transition when live is on", () => {
    const view = renderRecording({ rows: EMPTY, live: true });
    view.drain();

    view.rerender({ rows: FIRST_BATCH, live: true });

    // The starvation bug: any render returning [] here is a frame the operator
    // spends staring at no chart, with no upper bound under CPU contention.
    const sequence = view.drain();
    expect(sequence).not.toContain(EMPTY);
    expect(sequence.at(0)).toBe(FIRST_BATCH);
  });

  it("does not defer the empty to populated transition when live is off", () => {
    const view = renderRecording({ rows: EMPTY, live: false });
    view.drain();

    view.rerender({ rows: FIRST_BATCH, live: false });

    expect(view.drain().at(0)).toBe(FIRST_BATCH);
  });

  it("resumes deferring live ticks after the first rows arrive", () => {
    const view = renderRecording({ rows: EMPTY, live: true });
    view.rerender({ rows: FIRST_BATCH, live: true });
    view.drain();

    view.rerender({ rows: SECOND_BATCH, live: true });

    const sequence = view.drain();
    expect(sequence.at(0)).toBe(FIRST_BATCH);
    expect(sequence.at(-1)).toBe(SECOND_BATCH);
  });

  it("bypasses the defer again when rows empty out and refill", () => {
    const view = renderRecording({ rows: FIRST_BATCH, live: true });
    view.rerender({ rows: EMPTY, live: true });
    view.drain();

    view.rerender({ rows: SECOND_BATCH, live: true });

    expect(view.drain()).not.toContain(EMPTY);
  });
});
