"use client";

import { useEffect, type RefObject } from "react";

/** Pixels of pointer travel before a gesture counts as a horizontal drag. */
const DRAG_THRESHOLD_PX = 10;

/**
 * Click-drag pans the scrollport horizontally (grab + drag). Wheel/trackpad
 * still scrolls vertically. Suppresses the click that would fire after a drag
 * so row/cell handlers do not run.
 *
 * Pointer capture starts only after the drag threshold so a plain click still
 * reaches row ``onClick`` handlers (chart/window sync). Clicks are suppressed
 * only when the scroll position actually changed (not on click jitter).
 */
export function useTableDragPan(
  scrollRef: RefObject<HTMLDivElement | null>,
  enabled: boolean,
): void {
  useEffect(() => {
    if (!enabled) return;
    const scrollEl = scrollRef.current;
    if (scrollEl === null) return;
    const root: HTMLDivElement = scrollEl;

    let active = false;
    let dragging = false;
    let didPan = false;
    let startX = 0;
    let originLeft = 0;
    let pointerId: number | null = null;

    function onPointerDown(event: PointerEvent): void {
      if (event.button !== 0) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("a, button, input, select, textarea, label")) return;
      // Selectable body rows own the gesture (chart/window sync). Starting a
      // drag-pan here would arm pointer-capture on tiny movement and swallow
      // the row click — especially on wide horizontally-scrollable tables.
      if (target?.closest("tbody tr[tabindex]")) return;
      // Do not capture yet — wait until movement exceeds the drag threshold
      // so plain clicks outside selectable rows still behave normally.
      active = true;
      dragging = false;
      didPan = false;
      startX = event.clientX;
      originLeft = root.scrollLeft;
      pointerId = event.pointerId;
    }

    function onPointerMove(event: PointerEvent): void {
      if (!active || pointerId !== event.pointerId) return;
      const dx = event.clientX - startX;
      if (!dragging && Math.abs(dx) > DRAG_THRESHOLD_PX) {
        dragging = true;
        root.setPointerCapture(event.pointerId);
        root.classList.add("cursor-grabbing");
        root.classList.remove("cursor-grab");
      }
      if (!dragging) return;
      const nextLeft = originLeft - dx;
      if (nextLeft !== root.scrollLeft) {
        root.scrollLeft = nextLeft;
        didPan = true;
      }
    }

    function onPointerUp(event: PointerEvent): void {
      if (!active || pointerId !== event.pointerId) return;
      active = false;
      if (dragging && root.hasPointerCapture(event.pointerId)) {
        root.releasePointerCapture(event.pointerId);
      }
      root.classList.remove("cursor-grabbing");
      root.classList.add("cursor-grab");
      pointerId = null;
      // ``didPan`` stays until click-capture so a post-drag click is suppressed.
    }

    function onClickCapture(event: MouseEvent): void {
      if (!didPan) {
        dragging = false;
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      didPan = false;
      dragging = false;
    }

    root.classList.add("cursor-grab");
    root.addEventListener("pointerdown", onPointerDown);
    root.addEventListener("pointermove", onPointerMove);
    root.addEventListener("pointerup", onPointerUp);
    root.addEventListener("pointercancel", onPointerUp);
    root.addEventListener("click", onClickCapture, true);

    return () => {
      root.classList.remove("cursor-grab", "cursor-grabbing");
      root.removeEventListener("pointerdown", onPointerDown);
      root.removeEventListener("pointermove", onPointerMove);
      root.removeEventListener("pointerup", onPointerUp);
      root.removeEventListener("pointercancel", onPointerUp);
      root.removeEventListener("click", onClickCapture, true);
    };
  }, [enabled, scrollRef]);
}
