"use client";

import { type RefObject, useEffect } from "react";

/**
 * Click-drag pans the scrollport horizontally (grab + drag). Wheel/trackpad
 * still scrolls vertically. Suppresses the click that would fire after a drag
 * so row/cell handlers do not run.
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
    let startX = 0;
    let originLeft = 0;
    let moved = false;

    function onPointerDown(event: PointerEvent): void {
      if (event.button !== 0) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("a, button, input, select, textarea, label")) return;
      active = true;
      moved = false;
      startX = event.clientX;
      originLeft = root.scrollLeft;
      root.setPointerCapture(event.pointerId);
      root.classList.add("cursor-grabbing");
      root.classList.remove("cursor-grab");
    }

    function onPointerMove(event: PointerEvent): void {
      if (!active) return;
      const dx = event.clientX - startX;
      if (Math.abs(dx) > 3) moved = true;
      root.scrollLeft = originLeft - dx;
    }

    function onPointerUp(event: PointerEvent): void {
      if (!active) return;
      active = false;
      root.releasePointerCapture(event.pointerId);
      root.classList.remove("cursor-grabbing");
      root.classList.add("cursor-grab");
    }

    function onClickCapture(event: MouseEvent): void {
      if (!moved) return;
      event.preventDefault();
      event.stopPropagation();
      moved = false;
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
