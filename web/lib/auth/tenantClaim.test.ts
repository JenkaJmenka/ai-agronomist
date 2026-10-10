import { describe, expect, it, vi } from "vitest";

import { resolveTenantClaim, type TenantClaimDeps } from "./tenantClaim";

function createDeps(lookupTenantId: TenantClaimDeps["lookupTenantId"]): TenantClaimDeps {
  return { lookupTenantId };
}

describe("resolveTenantClaim", () => {
  it("writes the claim on sign-in when the email is provisioned", async () => {
    const lookupTenantId = vi.fn(async () => "tenant_1");
    const token = await resolveTenantClaim(
      { token: {}, signInEmail: "grower@example.com", isSignIn: true },
      createDeps(lookupTenantId),
    );
    expect(token).toEqual({ tenantId: "tenant_1" });
    expect(lookupTenantId).toHaveBeenCalledExactlyOnceWith("grower@example.com");
  });

  it("leaves the token unchanged on sign-in when the email has no mapping", async () => {
    const lookupTenantId = vi.fn(async () => null);
    const token = await resolveTenantClaim(
      {
        token: { email: "unmapped@example.com" },
        signInEmail: "unmapped@example.com",
        isSignIn: true,
      },
      createDeps(lookupTenantId),
    );
    expect(token).toEqual({ email: "unmapped@example.com" });
  });

  it("backfills on a refresh where there is no signInEmail and no signIn trigger — the case that was broken", async () => {
    const lookupTenantId = vi.fn(async () => "tenant_7");
    const token = await resolveTenantClaim(
      { token: { email: "grower@example.com" }, signInEmail: undefined, isSignIn: false },
      createDeps(lookupTenantId),
    );
    expect(token).toEqual({ email: "grower@example.com", tenantId: "tenant_7" });
    expect(lookupTenantId).toHaveBeenCalledExactlyOnceWith("grower@example.com");
  });

  it("does not query the database on a refresh once the claim is already present", async () => {
    const lookupTenantId = vi.fn(async () => "tenant_should_not_be_used");
    const token = await resolveTenantClaim(
      { token: { email: "grower@example.com", tenantId: "tenant_1" }, isSignIn: false },
      createDeps(lookupTenantId),
    );
    expect(token).toEqual({ email: "grower@example.com", tenantId: "tenant_1" });
    expect(lookupTenantId).not.toHaveBeenCalled();
  });

  it("leaves the token unchanged on a refresh when the lookup returns null — never newly invalidates", async () => {
    const lookupTenantId = vi.fn(async () => null);
    const originalToken = { email: "revoked@example.com", sub: "user-1" };
    const token = await resolveTenantClaim(
      { token: originalToken, isSignIn: false },
      createDeps(lookupTenantId),
    );
    expect(token).toEqual(originalToken);
  });

  it("does not query the database on a refresh when the token has no email to look up", async () => {
    const lookupTenantId = vi.fn(async () => "tenant_1");
    const token = await resolveTenantClaim(
      { token: { sub: "user-1" }, isSignIn: false },
      createDeps(lookupTenantId),
    );
    expect(token).toEqual({ sub: "user-1" });
    expect(lookupTenantId).not.toHaveBeenCalled();
  });
});
