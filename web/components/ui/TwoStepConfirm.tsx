"use client";

import { type ReactNode, useState } from "react";

import { Button } from "@/components/ui/Button";

interface TwoStepConfirmProps {
  /** Label of the initial trigger button. */
  readonly triggerLabel: string;
  /** Warning shown in the confirm step. */
  readonly warning: ReactNode;
  /** Label of the destructive confirm button. */
  readonly confirmLabel: string;
  readonly onConfirm: () => void;
  readonly isBusy?: boolean;
  readonly disabled?: boolean;
  readonly triggerVariant?: "primary" | "secondary";
  /** Test-id prefix: `{testId}-trigger`, `{testId}-confirm`, `{testId}-cancel`. */
  readonly testId: string;
}

/**
 * Two-step destructive confirm (plan §8 P4 gate): the first click arms the
 * action and shows the warning; only the explicit second click fires it.
 */
export function TwoStepConfirm({
  triggerLabel,
  warning,
  confirmLabel,
  onConfirm,
  isBusy = false,
  disabled = false,
  triggerVariant = "secondary",
  testId,
}: TwoStepConfirmProps) {
  const [isArmed, setIsArmed] = useState(false);

  if (!isArmed) {
    return (
      <Button
        variant={triggerVariant}
        disabled={disabled || isBusy}
        onClick={() => setIsArmed(true)}
        data-testid={`${testId}-trigger`}
      >
        {triggerLabel}
      </Button>
    );
  }

  return (
    <div
      className="space-y-3 rounded-[var(--radius-control)] border border-sev-critical/40 bg-sev-critical/5 p-3"
      role="alertdialog"
      aria-label={confirmLabel}
    >
      <p className="text-sm text-sev-critical">{warning}</p>
      <div className="flex gap-2">
        <Button
          variant="primary"
          className="bg-sev-critical"
          disabled={isBusy}
          onClick={() => {
            onConfirm();
            setIsArmed(false);
          }}
          data-testid={`${testId}-confirm`}
        >
          {isBusy ? "Working…" : confirmLabel}
        </Button>
        <Button
          variant="secondary"
          disabled={isBusy}
          onClick={() => setIsArmed(false)}
          data-testid={`${testId}-cancel`}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
