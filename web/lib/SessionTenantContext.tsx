"use client";

import { createContext, type ReactNode, useContext } from "react";

/**
 * The signed-in session's tenant, threaded down from `app/layout.tsx`
 * (a server component that calls `auth()`) through `app/providers.tsx`.
 * `useScope()` reads this as its default instead of a hardcoded tenant
 * literal — the missing piece behind the phase-2 auth 403s: routes that
 * take tenant from a URL path segment never got the BFF's session-tenant
 * overwrite, so the browser has to send the right tenant to begin with.
 */
const SessionTenantContext = createContext<string | null>(null);

export function SessionTenantProvider({
  tenantId,
  children,
}: {
  readonly tenantId: string | null;
  readonly children: ReactNode;
}) {
  return <SessionTenantContext value={tenantId}>{children}</SessionTenantContext>;
}

/** Null when there is no session — proxy.ts's fail-closed gate means this
 *  should never actually happen for a mounted page, but callers must not
 *  crash on it (e.g. a render that briefly happens during sign-out). */
export function useSessionTenantId(): string | null {
  return useContext(SessionTenantContext);
}
