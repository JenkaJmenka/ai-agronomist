import { describe, expect, it } from "vitest";

import { overwriteTenantIdInJsonBody } from "./tenantBodyRewrite";

describe("overwriteTenantIdInJsonBody", () => {
  it("overwrites an existing tenant_id field with the session tenant", () => {
    const rawBody = JSON.stringify({ tenant_id: "tenant_1", note: "hello" });
    const result = overwriteTenantIdInJsonBody(rawBody, "tenant_2");
    expect(JSON.parse(result)).toEqual({ tenant_id: "tenant_2", note: "hello" });
  });

  it("never adds tenant_id to a body that didn't already have it", () => {
    const rawBody = JSON.stringify({ note: "hello" });
    const result = overwriteTenantIdInJsonBody(rawBody, "tenant_2");
    expect(JSON.parse(result)).toEqual({ note: "hello" });
  });

  it("passes malformed JSON through unchanged rather than throwing", () => {
    const rawBody = "{not valid json";
    expect(overwriteTenantIdInJsonBody(rawBody, "tenant_2")).toBe(rawBody);
  });

  it("passes a JSON array through unchanged (not an object with fields)", () => {
    const rawBody = JSON.stringify([{ tenant_id: "tenant_1" }]);
    expect(overwriteTenantIdInJsonBody(rawBody, "tenant_2")).toBe(rawBody);
  });

  it("passes an empty body through unchanged", () => {
    expect(overwriteTenantIdInJsonBody("", "tenant_2")).toBe("");
  });

  it("passes null through unchanged", () => {
    expect(overwriteTenantIdInJsonBody("null", "tenant_2")).toBe("null");
  });
});
