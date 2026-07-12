"use client";

import { useState } from "react";

const WEEKDAY_LABELS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"] as const;

/** Google Material Symbols "chevron_left" / "chevron_right", 24px viewBox. */
function ChevronIcon({ direction }: { readonly direction: "left" | "right" }) {
  const path =
    direction === "left"
      ? "M560-240 320-480l240-240 56 56-184 184 184 184-56 56Z"
      : "M504-480 320-664l56-56 240 240-240 240-56-56 184-184Z";
  return (
    <svg width="16" height="16" viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true">
      <path d={path} />
    </svg>
  );
}

function startOfMonth(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function addMonths(date: Date, delta: number): Date {
  return new Date(date.getFullYear(), date.getMonth() + delta, 1);
}

function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/** Calendar cells for the month: leading nulls pad to a Monday start. */
function monthCells(viewMonth: Date): (Date | null)[] {
  const first = startOfMonth(viewMonth);
  const leadingBlanks = (first.getDay() + 6) % 7;
  const daysInMonth = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  const cells: (Date | null)[] = Array.from({ length: leadingBlanks }, () => null);
  for (let day = 1; day <= daysInMonth; day += 1) {
    cells.push(new Date(first.getFullYear(), first.getMonth(), day));
  }
  return cells;
}

function dayButtonClasses(isSelected: boolean, isToday: boolean): string {
  if (isSelected) return "bg-accent text-white font-semibold";
  if (isToday) return "border border-accent text-accent";
  return "text-text hover:bg-border/50";
}

interface CalendarDropdownProps {
  readonly value: Date | null;
  readonly onSelect: (day: Date) => void;
  readonly testId?: string;
}

/**
 * Token-styled month calendar (identical in light/dark — replaces the native
 * picker popup). Picking a day reports the date; time is the caller's concern.
 */
export function CalendarDropdown({ value, onSelect, testId }: CalendarDropdownProps) {
  const [viewMonth, setViewMonth] = useState<Date>(() => startOfMonth(value ?? new Date()));
  const today = new Date();
  const monthLabel = viewMonth.toLocaleDateString("en-GB", { month: "long", year: "numeric" });

  return (
    <div
      data-testid={testId}
      className="w-64 rounded-[var(--radius-card)] border border-border bg-bg-elevated p-3 shadow-[var(--shadow-elevation)]"
    >
      <div className="mb-2 flex items-center justify-between">
        <button
          type="button"
          aria-label="Previous month"
          onClick={() => setViewMonth((m) => addMonths(m, -1))}
          className="inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-control)] text-text-muted hover:bg-border/50 hover:text-text"
        >
          <ChevronIcon direction="left" />
        </button>
        <p className="text-sm font-semibold">{monthLabel}</p>
        <button
          type="button"
          aria-label="Next month"
          onClick={() => setViewMonth((m) => addMonths(m, 1))}
          className="inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-control)] text-text-muted hover:bg-border/50 hover:text-text"
        >
          <ChevronIcon direction="right" />
        </button>
      </div>
      <div className="grid grid-cols-7 gap-0.5 text-center">
        {WEEKDAY_LABELS.map((label) => (
          <span key={label} className="py-1 text-[11px] font-semibold text-text-muted">
            {label}
          </span>
        ))}
        {monthCells(viewMonth).map((day, index) =>
          day === null ? (
            // biome-ignore lint/suspicious/noArrayIndexKey: blank pad cells have no identity
            <span key={`blank-${index}`} />
          ) : (
            <button
              key={day.toISOString()}
              type="button"
              onClick={() => onSelect(day)}
              aria-pressed={value !== null && isSameDay(day, value)}
              className={`h-8 rounded-[var(--radius-control)] text-sm tabular-nums transition-colors ${dayButtonClasses(
                value !== null && isSameDay(day, value),
                isSameDay(day, today),
              )}`}
            >
              {day.getDate()}
            </button>
          ),
        )}
      </div>
    </div>
  );
}
