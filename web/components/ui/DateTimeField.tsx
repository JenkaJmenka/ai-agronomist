"use client";

import type { ChangeEvent, KeyboardEvent } from "react";
import { useEffect, useRef, useState } from "react";

import { CalendarDropdown } from "@/components/ui/CalendarDropdown";

type SegmentKey = "day" | "month" | "year" | "hour" | "minute";

interface SegmentSpec {
  readonly key: SegmentKey;
  readonly length: number;
  readonly placeholder: string;
  readonly min: number;
  readonly max: number;
}

/** Caret order: day → month → year → hour → minute (auto-advance on fill). */
const SEGMENTS: readonly SegmentSpec[] = [
  { key: "day", length: 2, placeholder: "DD", min: 1, max: 31 },
  { key: "month", length: 2, placeholder: "MM", min: 1, max: 12 },
  { key: "year", length: 4, placeholder: "YYYY", min: 1970, max: 9999 },
  { key: "hour", length: 2, placeholder: "HH", min: 0, max: 23 },
  { key: "minute", length: 2, placeholder: "MM", min: 0, max: 59 },
];

/** Separator rendered before the segment at the same index. */
const SEPARATORS: readonly (string | null)[] = [null, ".", ".", " ", ":"];

type SegmentValues = Readonly<Record<SegmentKey, string>>;

const EMPTY_VALUES: SegmentValues = { day: "", month: "", year: "", hour: "", minute: "" };

function toSegmentValues(date: Date | null): SegmentValues {
  if (!date) return EMPTY_VALUES;
  return {
    day: String(date.getDate()).padStart(2, "0"),
    month: String(date.getMonth() + 1).padStart(2, "0"),
    year: String(date.getFullYear()).padStart(4, "0"),
    hour: String(date.getHours()).padStart(2, "0"),
    minute: String(date.getMinutes()).padStart(2, "0"),
  };
}

function isSegmentOutOfRange(spec: SegmentSpec, raw: string): boolean {
  if (raw.length < spec.length) return false; // still typing
  const numeric = Number(raw);
  return numeric < spec.min || numeric > spec.max;
}

/** null while incomplete or when segments don't form a real calendar date. */
function toDate(values: SegmentValues): Date | null {
  const isComplete = SEGMENTS.every(
    (spec) =>
      values[spec.key].length === spec.length && !isSegmentOutOfRange(spec, values[spec.key]),
  );
  if (!isComplete) return null;
  const day = Number(values.day);
  const month = Number(values.month);
  const date = new Date(
    Number(values.year),
    month - 1,
    day,
    Number(values.hour),
    Number(values.minute),
  );
  // Rollover (e.g. Feb 30 → Mar 2) means the day never existed.
  const isRealDay = date.getDate() === day && date.getMonth() === month - 1;
  return isRealDay ? date : null;
}

/** Google Material Symbols "calendar_month", 24px viewBox. */
function CalendarIcon() {
  return (
    <svg width="16" height="16" viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true">
      <path d="M200-80q-33 0-56.5-23.5T120-160v-560q0-33 23.5-56.5T200-800h40v-80h80v80h320v-80h80v80h40q33 0 56.5 23.5T840-720v560q0 33-23.5 56.5T760-80H200Zm0-80h560v-400H200v400Zm0-480h560v-80H200v80Zm0 0v-80 80ZM480-400q-17 0-28.5-11.5T440-440q0-17 11.5-28.5T480-480q17 0 28.5 11.5T520-440q0 17-11.5 28.5T480-400Zm-160 0q-17 0-28.5-11.5T280-440q0-17 11.5-28.5T320-480q17 0 28.5 11.5T360-440q0 17-11.5 28.5T320-400Zm320 0q-17 0-28.5-11.5T600-440q0-17 11.5-28.5T640-480q17 0 28.5 11.5T680-440q0 17-11.5 28.5T640-400ZM480-240q-17 0-28.5-11.5T440-280q0-17 11.5-28.5T480-320q17 0 28.5 11.5T520-280q0 17-11.5 28.5T480-240Zm-160 0q-17 0-28.5-11.5T280-280q0-17 11.5-28.5T320-320q17 0 28.5 11.5T360-280q0 17-11.5 28.5T320-240Zm320 0q-17 0-28.5-11.5T600-280q0-17 11.5-28.5T640-320q17 0 28.5 11.5T680-280q0 17-11.5 28.5T640-240Z" />
    </svg>
  );
}

interface DateTimeFieldProps {
  readonly value: Date | null;
  readonly onChange: (value: Date | null) => void;
  /** External validity (e.g. range ordering) — paints the same error border. */
  readonly isInvalid?: boolean;
  /** Group label, e.g. "From" — segments carry their own aria-labels. */
  readonly label: string;
  readonly testId?: string;
}

/**
 * Segmented date-time input (DD.MM.YYYY HH:MM). Replaces `datetime-local`
 * so both themes share one design (no native picker), each segment hard-caps
 * its length (year: 4) and the caret auto-advances through hour and minute.
 */
export function DateTimeField({
  value,
  onChange,
  isInvalid = false,
  label,
  testId,
}: DateTimeFieldProps) {
  const [values, setValues] = useState<SegmentValues>(() => toSegmentValues(value));
  const [isCalendarOpen, setIsCalendarOpen] = useState(false);
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!isCalendarOpen) return;
    function handleDismiss(event: PointerEvent | globalThis.KeyboardEvent): void {
      const isOutsideClick =
        event instanceof PointerEvent &&
        event.target instanceof Node &&
        !containerRef.current?.contains(event.target);
      const isEscape = "key" in event && event.key === "Escape";
      if (isOutsideClick || isEscape) setIsCalendarOpen(false);
    }
    document.addEventListener("pointerdown", handleDismiss);
    document.addEventListener("keydown", handleDismiss);
    return () => {
      document.removeEventListener("pointerdown", handleDismiss);
      document.removeEventListener("keydown", handleDismiss);
    };
  }, [isCalendarOpen]);

  const hasSegmentError =
    SEGMENTS.some((spec) => isSegmentOutOfRange(spec, values[spec.key])) ||
    (SEGMENTS.every((spec) => values[spec.key].length === spec.length) && toDate(values) === null);
  const showError = isInvalid || hasSegmentError;

  function handleSegmentChange(index: number, event: ChangeEvent<HTMLInputElement>): void {
    const spec = SEGMENTS[index];
    const digits = event.target.value.replace(/\D/g, "").slice(0, spec.length);
    const next = { ...values, [spec.key]: digits };
    setValues(next);
    onChange(toDate(next));
    if (digits.length === spec.length) inputRefs.current[index + 1]?.select();
  }

  function handleSegmentKeyDown(index: number, event: KeyboardEvent<HTMLInputElement>): void {
    const isEmpty = event.currentTarget.value === "";
    if (event.key === "Backspace" && isEmpty) {
      event.preventDefault();
      inputRefs.current[index - 1]?.select();
    }
  }

  function handleCalendarSelect(picked: Date): void {
    const next: SegmentValues = {
      day: String(picked.getDate()).padStart(2, "0"),
      month: String(picked.getMonth() + 1).padStart(2, "0"),
      year: String(picked.getFullYear()).padStart(4, "0"),
      // Keep any typed time; a fresh pick defaults to midnight.
      hour: values.hour.length === 2 ? values.hour : "00",
      minute: values.minute.length === 2 ? values.minute : "00",
    };
    setValues(next);
    onChange(toDate(next));
    setIsCalendarOpen(false);
  }

  return (
    <div ref={containerRef} className="relative mt-1 w-max">
      <fieldset
        aria-label={label}
        data-testid={testId}
        aria-invalid={showError || undefined}
        className={`flex h-9 w-max items-center rounded-[var(--radius-control)] border bg-bg px-2 text-sm text-text transition-colors focus-within:outline-2 focus-within:outline-accent ${
          showError ? "border-sev-critical" : "border-border"
        }`}
      >
        {SEGMENTS.map((spec, index) => (
          <span key={spec.key} className="flex items-center">
            {SEPARATORS[index] ? (
              <span aria-hidden="true" className="text-text-muted">
                {SEPARATORS[index]}
              </span>
            ) : null}
            <input
              ref={(el) => {
                inputRefs.current[index] = el;
              }}
              type="text"
              inputMode="numeric"
              autoComplete="off"
              maxLength={spec.length}
              placeholder={spec.placeholder}
              aria-label={spec.key}
              value={values[spec.key]}
              onChange={(e) => handleSegmentChange(index, e)}
              onKeyDown={(e) => handleSegmentKeyDown(index, e)}
              onFocus={(e) => e.target.select()}
              className={`bg-transparent text-center tabular-nums outline-none placeholder:text-text-muted/50 ${
                spec.length === 4 ? "w-[4.5ch]" : "w-[2.5ch]"
              }`}
              data-testid={testId ? `${testId}-${spec.key}` : undefined}
            />
          </span>
        ))}
        <button
          type="button"
          aria-label={`Open ${label} calendar`}
          aria-expanded={isCalendarOpen}
          onClick={() => setIsCalendarOpen((open) => !open)}
          className="ml-1.5 inline-flex h-6 w-6 items-center justify-center rounded-[var(--radius-control)] text-text-muted transition-colors hover:bg-border/50 hover:text-text"
          data-testid={testId ? `${testId}-calendar-toggle` : undefined}
        >
          <CalendarIcon />
        </button>
      </fieldset>
      {isCalendarOpen && (
        <div className="absolute left-0 top-full z-30 mt-1">
          <CalendarDropdown
            value={toDate(values)}
            onSelect={handleCalendarSelect}
            testId={testId ? `${testId}-calendar` : undefined}
          />
        </div>
      )}
    </div>
  );
}
