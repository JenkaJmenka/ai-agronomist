import { renderHook } from "@testing-library/react";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { SessionTenantProvider } from "@/lib/SessionTenantContext";
import { type Scope, useScope } from "@/lib/useScope";

/**
 * Regression coverage for the production 403s on tenant-in-path routes
 * (`/api/bff/models/${tenant}/versions` and friends): `useScope()` used to
 * hardcode `"tenant_1"` as its tenant default, so every operator on another
 * tenant sent someone else's tenant in the URL path — where, unlike a query
 * param, the BFF's session-tenant overwrite never reaches it.
 */

const NO_PROVIDER = Symbol("no SessionTenantProvider");

function renderScope(options: {
  readonly searchParams?: string;
  readonly sessionTenantId?: string | null | typeof NO_PROVIDER;
}): Scope {
  const { searchParams = "", sessionTenantId = NO_PROVIDER } = options;

  function wrapper({ children }: { readonly children: ReactNode }) {
    return (
      <NuqsTestingAdapter searchParams={searchParams}>
        {sessionTenantId === NO_PROVIDER ? (
          children
        ) : (
          <SessionTenantProvider tenantId={sessionTenantId}>{children}</SessionTenantProvider>
        )}
      </NuqsTestingAdapter>
    );
  }

  return renderHook(() => useScope(), { wrapper }).result.current;
}

describe("useScope — tenant default", () => {
  it("should default to the signed-in session's tenant", () => {
    expect(renderScope({ sessionTenantId: "tenant_7" }).tenant).toBe("tenant_7");
  });

  it("should fall back to tenant_1 when there is no session tenant provider", () => {
    expect(renderScope({}).tenant).toBe("tenant_1");
  });

  it("should fall back to tenant_1 when the session tenant is null", () => {
    expect(renderScope({ sessionTenantId: null }).tenant).toBe("tenant_1");
  });

  it("should let a ?tenant= URL value override the session tenant", () => {
    const scope = renderScope({ searchParams: "?tenant=tenant_9", sessionTenantId: "tenant_7" });

    expect(scope.tenant).toBe("tenant_9");
  });

  it("should let a ?tenant= URL value override the no-session fallback", () => {
    expect(renderScope({ searchParams: "?tenant=tenant_9" }).tenant).toBe("tenant_9");
  });
});

describe("useScope — greenhouse", () => {
  it("should default the greenhouse to gh_A regardless of the session tenant", () => {
    expect(renderScope({ sessionTenantId: "tenant_7" }).gh).toBe("gh_A");
  });

  it("should read the greenhouse from ?gh= without disturbing the tenant default", () => {
    const scope = renderScope({ searchParams: "?gh=gh_B", sessionTenantId: "tenant_7" });

    expect(scope).toEqual({ tenant: "tenant_7", gh: "gh_B" });
  });
});
