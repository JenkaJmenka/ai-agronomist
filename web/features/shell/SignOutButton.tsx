"use client";

import { useState, useTransition } from "react";

import { Button } from "@/components/ui/Button";
import { signOutAction } from "@/features/shell/signOutAction";

/**
 * Sign-out control for the sidebar header, next to `ThemeToggle` and above
 * `GreenhouseSelector` — previously the only way to end a session was
 * navigating to `/api/auth/signout` by hand. On success the action redirects
 * to `/login`; this component never does, so a failure (surfaced below the
 * row, not thrown) always leaves the still-authenticated UI intact rather
 * than half-signed-out.
 */
export function SignOutButton() {
  const [isPending, startTransition] = useTransition();
  const [hasFailed, setHasFailed] = useState(false);

  function handleSignOut(): void {
    setHasFailed(false);
    startTransition(async () => {
      const result = await signOutAction();
      if (result && !result.ok) {
        setHasFailed(true);
      }
    });
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={handleSignOut}
        disabled={isPending}
        data-testid="sign-out-button"
      >
        {isPending ? "Signing out…" : "Sign out"}
      </Button>
      {hasFailed ? (
        <p role="alert" className="text-xs text-sev-critical">
          Sign-out failed. Please try again.
        </p>
      ) : null}
    </div>
  );
}
