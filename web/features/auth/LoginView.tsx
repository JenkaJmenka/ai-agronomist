import { AuthError } from "next-auth";

import { signIn } from "@/auth";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";

interface LoginViewProps {
  readonly callbackUrl?: string;
}

/**
 * Magic-link request form (security phase 2, §1). Submitting always shows
 * the same confirmation regardless of whether the email is provisioned —
 * the `signIn` callback in `auth.ts` denies unknown emails, but surfacing
 * that here would let a caller enumerate which emails have accounts.
 */
export function LoginView({ callbackUrl }: LoginViewProps) {
  async function requestMagicLink(formData: FormData): Promise<void> {
    "use server";
    const email = String(formData.get("email") ?? "").trim();
    try {
      await signIn("resend", { email, redirectTo: callbackUrl ?? "/" });
    } catch (error) {
      // AuthError covers both "denied" (unknown email) and provider send
      // failures — both render the same "check your email" outcome.
      if (error instanceof AuthError) return;
      throw error;
    }
  }

  return (
    <div className="mx-auto flex min-h-[60vh] max-w-sm items-center">
      <Card title="Sign in" className="w-full">
        <p className="mb-4 text-sm text-text-muted">
          Enter your work email — we'll send you a sign-in link.
        </p>
        <form action={requestMagicLink} className="flex flex-col gap-3">
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
          <Button type="submit" data-testid="login-submit">
            Send sign-in link
          </Button>
        </form>
      </Card>
    </div>
  );
}
