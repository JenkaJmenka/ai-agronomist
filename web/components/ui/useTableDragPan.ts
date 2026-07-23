"use client";

import { type RefObject, useEffect } from "react";

/**
 * Click-drag pans the scrollport horizontally (grab + drag). Wheel/trackpad
 * still scrolls vertically. Suppresses the click that would fire after a drag
 * so row/cell handlers do not run.
 *
 * Important: pointer capture starts only after the drag threshold so a plain
 * click still reaches row ``onClick`` handlers (chart/window sync).
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
    let startX = 0;
    let originLeft = 0;
    let pointerId: number | null = null;

    function onPointerDown(event: PointerEvent): void {
      if (event.button !== 0) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("a, button, input, select, textarea, label")) return;
      // Do not capture yet — wait until movement exceeds the drag threshold
      // so row clicks (selection → chart window) still fire.
      active = true;
      dragging = false;
      startX = event.clientX;
      originLeft = root.scrollLeft;
      pointerId = event.pointerId;
    }

    function onPointerMove(event: PointerEvent): void {
      if (!active || pointerId !== event.pointerId) return;
      const dx = event.clientX - startX;
      if (!dragging && Math.abs(dx) > 3) {
        dragging = true;
        root.setPointerCapture(event.pointerId);
        root.classList.add("cursor-grabbing");
        root.classList.remove("cursor-grab");
      }
      if (!dragging) return;
      root.scrollLeft = originLeft - dx;
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
      // ``dragging`` stays true until click-capture so the synthetic click
      // after a drag is suppressed; reset there.
    }

    function onClickCapture(event: MouseEvent): void {
      if (!dragging) return;
      event.preventDefault();
      event.stopPropagation();
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
