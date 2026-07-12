import type uPlot from "uplot";

export type RangeSelectHandler = (from: Date, to: Date) => void;

/**
 * uPlot `setSelect` hook: converts a drag-selection on the x-axis into a
 * [from, to] Date pair. Callers push it into the URL window so every
 * range-driven view (chart *and* table) filters to the selection.
 */
export function createRangeSelectHook(onRangeSelect: RangeSelectHandler): (u: uPlot) => void {
  return (u) => {
    if (u.select.width <= 0) return;
    const fromSec = u.posToVal(u.select.left, "x");
    const toSec = u.posToVal(u.select.left + u.select.width, "x");
    if (!Number.isFinite(fromSec) || !Number.isFinite(toSec) || toSec <= fromSec) return;
    onRangeSelect(new Date(fromSec * 1000), new Date(toSec * 1000));
  };
}

/** Drag config for selection-driven charts: select, don't self-zoom — the
 * URL window update refetches and re-renders the chart instead. */
export const SELECT_ONLY_CURSOR: uPlot.Cursor = {
  drag: { x: true, y: false, setScale: false },
};
