"use client";

import type { FormEvent } from "react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";

interface CustomDateRangeFormProps {
  readonly from: Date | null;
  readonly to: Date | null;
  readonly onApply: (from: Date, to: Date) => void;
}

const INPUT_CLASSES =
  "mt-1 block rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm text-text";

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

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!isOrdered) return;
    onApply(new Date(fromValue), new Date(toValue));
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-wrap items-end gap-3"
      data-testid="custom-range-form"
    >
      <label className="text-sm">
        <span className="text-text-muted">From</span>
        <input
          type="datetime-local"
          value={fromValue}
          max={toValue || undefined}
          onChange={(e) => setFromValue(e.target.value)}
          className={INPUT_CLASSES}
          data-testid="custom-range-from"
        />
      </label>
      <label className="text-sm">
        <span className="text-text-muted">To</span>
        <input
          type="datetime-local"
          value={toValue}
          min={fromValue || undefined}
          onChange={(e) => setToValue(e.target.value)}
          className={INPUT_CLASSES}
          data-testid="custom-range-to"
        />
      </label>
      <Button
        type="submit"
        variant="secondary"
        size="sm"
        disabled={!isOrdered}
        data-testid="custom-range-apply"
      >
        Apply
      </Button>
      {hasBothValues && !isOrdered && (
        <p role="alert" className="text-xs text-sev-critical">
          “From” must be before “To”.
        </p>
      )}
    </form>
  );
}
