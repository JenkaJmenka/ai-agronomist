"use client";

import { parseAsBoolean, useQueryState } from "nuqs";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { CustomDateRangeForm } from "@/components/ui/CustomDateRangeForm";
import { fetchReadingsBounds } from "@/features/monitoring/api/readings";
import {
  DEFAULT_PERIOD_ID,
  matchPeriodId,
  PERIOD_PRESETS,
  periodHours,
  periodWindowEnd,
  REFRESH_PRESETS,
  windowForPeriod,
} from "@/lib/periodPresets";
import { timed } from "@/lib/profiling";
import { useDateRange } from "@/lib/useDateRange";
import { useRefreshInterval } from "@/lib/useRefreshInterval";
import { useScope } from "@/lib/useScope";

const SELECT_CLASS =
  "h-8 rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 text-sm text-text";

/**
 * Period + refresh dropdowns (URL-driven). Replaces the old chip row so
 * Setpoints / Go Live share the same baseline as these controls.
 *
 * Preset periods anchor to the newest stored reading (not wall-clock now),
 * matching `/api/readings/recent`, so historical uploads fill Last hour/24h/7d.
 * While Go Live is on, the end prefers wall-clock so incoming ticks stay in view.
 */
export function DateRangeControls() {
  const { tenant, gh } = useScope();
  const [{ from, to }, setRange] = useDateRange();
  const [live] = useQueryState("live", parseAsBoolean.withDefault(false));
  const [refresh, setRefresh] = useRefreshInterval();
  const [isCustomOpen, setIsCustomOpen] = useState(false);
  /**
   * When Custom is already the selected value, `<select onChange>` does not
   * fire if the user picks Custom again. Briefly clear the value while the
   * menu is open so choosing Custom always reopens the datetime pickers.
   */
  const [armCustomReselect, setArmCustomReselect] = useState(false);

  const matched = from && to ? matchPeriodId(from, to) : "";
  const periodValue = isCustomOpen || matched === "custom" ? "custom" : matched;
  const selectValue = armCustomReselect ? "" : periodValue;

  async function applyPeriod(periodId: string): Promise<void> {
    if (periodId === "custom") {
      setIsCustomOpen(true);
      return;
    }
    if (!periodId) return;
    await timed(
      "ui.time_window",
      async () => {
        setIsCustomOpen(false);
        const hours = periodHours(periodId) ?? periodHours(DEFAULT_PERIOD_ID) ?? 24;

        // Optimistic wall-clock snap so the select click is not blocked on bounds.
        await setRange(windowForPeriod(hours, periodWindowEnd({ maxTimestamp: null, live })));

        try {
          const bounds = await fetchReadingsBounds({ tenant, gh });
          if (!bounds.max_timestamp) return;
          const end = periodWindowEnd({
            maxTimestamp: new Date(bounds.max_timestamp),
            live,
          });
          await setRange(windowForPeriod(hours, end));
        } catch {
          // Keep the optimistic wall-clock window.
        }
      },
      { period: periodId },
    );
  }

  function applyCustomRange(start: Date, end: Date): void {
    setIsCustomOpen(false);
    void timed(
      "ui.time_window",
      async () => {
        await setRange({ from: start, to: end });
      },
      { period: "custom" },
    );
  }

  function handlePeriodMouseDown(): void {
    // Only Custom needs re-select-to-reopen; presets already change the value.
    if (periodValue === "custom") {
      setArmCustomReselect(true);
    }
  }

  function handlePeriodChange(value: string): void {
    setArmCustomReselect(false);
    void applyPeriod(value);
  }

  function handlePeriodBlur(): void {
    // If the menu closed without a change, restore the displayed Custom value.
    setArmCustomReselect(false);
  }

  return (
    <div className="relative flex items-center gap-2" data-testid="date-range-controls">
      <label className="flex items-center gap-1.5 text-sm text-text-muted">
        <span className="sr-only">Period</span>
        <select
          className={SELECT_CLASS}
          value={selectValue}
          onMouseDown={handlePeriodMouseDown}
          onBlur={handlePeriodBlur}
          onChange={(e) => handlePeriodChange(e.target.value)}
          data-testid="period-select"
          aria-label="Time period"
        >
          <option value="" disabled>
            Period
          </option>
          {PERIOD_PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
          <option value="custom">Custom</option>
        </select>
      </label>

      <label className="flex items-center gap-1.5 text-sm text-text-muted">
        <span className="sr-only">Refresh</span>
        <select
          className={SELECT_CLASS}
          value={String(refresh)}
          onChange={(e) => void setRefresh(Number(e.target.value))}
          data-testid="refresh-select"
          aria-label="Refresh interval"
        >
          {REFRESH_PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              Refresh {p.label}
            </option>
          ))}
        </select>
      </label>

      {isCustomOpen && (
        <div className="absolute right-0 top-full z-20 mt-1 w-max">
          <CustomDateRangeForm from={from} to={to} onApply={applyCustomRange} />
          <div className="mt-1 flex justify-end">
            <Button variant="ghost" size="sm" onClick={() => setIsCustomOpen(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
