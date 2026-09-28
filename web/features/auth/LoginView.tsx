"use client";

import { useActionState } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { requestMagicLinkAction } from "@/features/auth/actions";

interface LoginViewProps {
  readonly callbackUrl?: string;
}

/**
 * Magic-link request form (security phase 2, §1 + magic-link hardening).
 * Submitting always shows the identical confirmation regardless of whether
 * the email is provisioned or rate limited — never a redirect, never
 * different text — so nothing here lets a caller tell those cases apart.
 * `features/auth/actions.ts` carries the actual rate-limit + send logic.
 */
export function LoginView({ callbackUrl }: LoginViewProps) {
  const boundAction = requestMagicLinkAction.bind(null, callbackUrl ?? "/");
  const [confirmation, formAction, isPending] = useActionState(boundAction, null);

  return (
    <div className="mx-auto flex min-h-[60vh] max-w-sm items-center">
      <Card title="Sign in" className="w-full">
        {confirmation ? (
          <p data-testid="login-confirmation" className="text-sm text-text">
            {confirmation}
          </p>
        ) : (
          <>
            <p className="mb-4 text-sm text-text-muted">
              Enter your work email — we'll send you a sign-in link.
            </p>
            <form action={formAction} className="flex flex-col gap-3">
              <label className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                Email
                <input
                  data-testid="login-email"
                  type="email"
                  name="email"
                  required
                  className="mt-1 block w-full rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm text-text"
                />
              </label>
              <Button type="submit" disabled={isPending} data-testid="login-submit">
                Send sign-in link
              </Button>
            </form>
          </>
        )}
      </Card>
    </div>
  );
}
