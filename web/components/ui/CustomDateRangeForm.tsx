"use client";

import type { FormEvent } from "react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";

interface CustomDateRangeFormProps {
  readonly from: Date | null;
  readonly to: Date | null;
  readonly onApply: (from: Date, to: Date) => void;
}

const INPUT_BASE_CLASSES =
  "mt-1 block h-9 rounded-[var(--radius-control)] border bg-bg px-2.5 text-sm text-text " +
  "transition-colors focus-visible:outline-2 focus-visible:outline-accent";

/** Format a Date as a `datetime-local` value (local time, minute precision). */
function toDatetimeLocalValue(date: Date | null): string {
  if (!date) return "";
  const localMs = date.getTime() - date.getTimezoneOffset() * 60_000;
  return new Date(localMs).toISOString().slice(0, 16);
}

/** Explicit from/to window picker; `datetime-local` values parse as local time. */
export function CustomDateRangeForm({ from, to, onApply }: CustomDateRangeFormProps) {
  const [fromValue, setFromValue] = useState(() => toDatetimeLocalValue(from));
  const [toValue, setToValue] = useState(() => toDatetimeLocalValue(to));

  const hasBothValues = fromValue !== "" && toValue !== "";
  const isOrdered = hasBothValues && new Date(fromValue) < new Date(toValue);
  const isInvalid = hasBothValues && !isOrdered;
  const inputClasses = `${INPUT_BASE_CLASSES} ${isInvalid ? "border-sev-critical" : "border-border"}`;

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!isOrdered) return;
    onApply(new Date(fromValue), new Date(toValue));
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-wrap items-end gap-x-3 gap-y-2 rounded-[var(--radius-card)] border border-border bg-bg-elevated p-3 shadow-[var(--shadow-elevation)]"
      data-testid="custom-range-form"
    >
      <label className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        From
        <input
          type="datetime-local"
          value={fromValue}
          max={toValue || undefined}
          aria-invalid={isInvalid || undefined}
          onChange={(e) => setFromValue(e.target.value)}
          className={inputClasses}
          data-testid="custom-range-from"
        />
      </label>
      <span className="pb-2 text-text-muted" aria-hidden="true">
        →
      </span>
      <label className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        To
        <input
          type="datetime-local"
          value={toValue}
          min={fromValue || undefined}
          aria-invalid={isInvalid || undefined}
          onChange={(e) => setToValue(e.target.value)}
          className={inputClasses}
          data-testid="custom-range-to"
        />
      </label>
      <Button type="submit" disabled={!isOrdered} data-testid="custom-range-apply">
        Apply
      </Button>
      {isInvalid && (
        <p role="alert" className="basis-full text-xs text-sev-critical">
          “From” must be before “To”.
        </p>
      )}
    </form>
  );
}
