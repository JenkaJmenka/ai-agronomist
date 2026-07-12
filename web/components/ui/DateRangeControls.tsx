"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { CustomDateRangeForm } from "@/components/ui/CustomDateRangeForm";
import { useDateRange } from "@/lib/useDateRange";

const QUICK_RANGES = [
  { label: "24 h", hours: 24 },
  { label: "7 d", hours: 24 * 7 },
  { label: "1 mo", hours: 24 * 30 },
  { label: "6 mo", hours: 24 * 182 },
] as const;

/**
 * URL-driven window controls (plan §7.4): state lives in the URL so reloads
 * and interrupted fetches are lossless — the structural fix for the
 * Streamlit state-loss bug.
 */
export function DateRangeControls() {
  const [{ from, to }, setRange] = useDateRange();
  const [isCustomOpen, setIsCustomOpen] = useState(false);

  function applyQuickRange(hours: number): void {
    const end = new Date();
    const start = new Date(end.getTime() - hours * 3_600_000);
    setIsCustomOpen(false);
    void setRange({ from: start, to: end });
  }

  function applyCustomRange(start: Date, end: Date): void {
    void setRange({ from: start, to: end });
  }

  return (
    <div className="relative flex flex-col items-end gap-1" data-testid="date-range-controls">
      <div className="flex flex-wrap items-center gap-2">
        {QUICK_RANGES.map((r) => (
          <Button
            key={r.label}
            variant="secondary"
            size="sm"
            onClick={() => applyQuickRange(r.hours)}
          >
            {r.label}
          </Button>
        ))}
        <Button
          variant={isCustomOpen ? "primary" : "secondary"}
          size="sm"
          aria-expanded={isCustomOpen}
          onClick={() => setIsCustomOpen((open) => !open)}
          data-testid="custom-range-toggle"
        >
          Custom
        </Button>
      </div>
      <span className="text-xs text-text-muted tabular-nums">
        {from && to
          ? `${from.toISOString().slice(0, 16)} → ${to.toISOString().slice(0, 16)}`
          : "No window selected"}
      </span>
      {isCustomOpen && (
        // Anchored dropdown: opening, typing, or applying never reflows the
        // header row, so the picker can't drift from right to left.
        <div className="absolute right-0 top-full z-20 mt-1 w-max">
          <CustomDateRangeForm from={from} to={to} onApply={applyCustomRange} />
        </div>
      )}
    </div>
  );
}
