"use client";

import type { ChangeEvent, KeyboardEvent } from "react";
import { useRef, useState } from "react";

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
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);

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

  return (
    <fieldset
      aria-label={label}
      data-testid={testId}
      aria-invalid={showError || undefined}
      className={`mt-1 flex h-9 w-max items-center rounded-[var(--radius-control)] border bg-bg px-2 text-sm text-text transition-colors focus-within:outline-2 focus-within:outline-accent ${
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
    </fieldset>
  );
}
