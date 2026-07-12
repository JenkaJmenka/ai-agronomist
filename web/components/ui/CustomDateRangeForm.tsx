"use client";

import type { FormEvent } from "react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { DateTimeField } from "@/components/ui/DateTimeField";

interface CustomDateRangeFormProps {
  readonly from: Date | null;
  readonly to: Date | null;
  readonly onApply: (from: Date, to: Date) => void;
}

/** Explicit from/to window picker built on segmented DateTimeField inputs. */
export function CustomDateRangeForm({ from, to, onApply }: CustomDateRangeFormProps) {
  const [fromValue, setFromValue] = useState<Date | null>(from);
  const [toValue, setToValue] = useState<Date | null>(to);

  const hasBothValues = fromValue !== null && toValue !== null;
  const isOrdered = hasBothValues && fromValue < toValue;
  const isInvalid = hasBothValues && !isOrdered;

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!isOrdered) return;
    onApply(fromValue, toValue);
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-wrap items-end gap-x-3 gap-y-2 rounded-[var(--radius-card)] border border-border bg-bg-elevated p-3 shadow-[var(--shadow-elevation)]"
      data-testid="custom-range-form"
    >
      <div className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        From
        <DateTimeField
          value={fromValue}
          onChange={setFromValue}
          isInvalid={isInvalid}
          label="From"
          testId="custom-range-from"
        />
      </div>
      <span className="pb-2 text-text-muted" aria-hidden="true">
        →
      </span>
      <div className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        To
        <DateTimeField
          value={toValue}
          onChange={setToValue}
          isInvalid={isInvalid}
          label="To"
          testId="custom-range-to"
        />
      </div>
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
