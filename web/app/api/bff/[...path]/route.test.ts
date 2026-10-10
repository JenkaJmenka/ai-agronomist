import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GET, POST } from "./route";

/**
 * Regression coverage for the production 403 bug (security phase 2
 * follow-up): a signed-in session's tenant must reach the API even when the
 * FE's `useScope()`-driven query params or a stale JSON body field disagree
 * with it — the BFF overwrites, the API no longer needs to reject a
 * mismatch it never sees. Assertions are on what is forwarded upstream
 * (the mocked `fetch` call), not on the response.
 */

function stubUpstreamFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("BFF catch-all proxy — tenant overwrite", () => {
  it("overwrites a mismatched tenant_id query param with the session tenant", async () => {
    const fetchMock = stubUpstreamFetch();
    const req = new NextRequest(
      "http://localhost/api/bff/readings/window?tenant_id=tenant_1&gh=gh_A",
      { headers: { "x-tenant-id": "tenant_2" } },
    );

    await GET(req, { params: Promise.resolve({ path: ["readings", "window"] }) });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [target, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(new URL(target.toString()).searchParams.get("tenant_id")).toBe("tenant_2");
    // Unrelated params pass through untouched.
    expect(new URL(target.toString()).searchParams.get("gh")).toBe("gh_A");
    expect((init.headers as Headers).get("X-Tenant-Id")).toBe("tenant_2");
  });

  it("does not add tenant_id to a route whose query string never had it", async () => {
    const fetchMock = stubUpstreamFetch();
    const req = new NextRequest("http://localhost/api/bff/monitoring/metrics", {
      headers: { "x-tenant-id": "tenant_2" },
    });

    await GET(req, { params: Promise.resolve({ path: ["monitoring", "metrics"] }) });

    const [target] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(new URL(target.toString()).searchParams.has("tenant_id")).toBe(false);
  });

  it("overwrites a mismatched tenant_id field in a JSON request body", async () => {
    const fetchMock = stubUpstreamFetch();
    const req = new NextRequest("http://localhost/api/bff/incidents/store", {
      method: "POST",
      headers: { "x-tenant-id": "tenant_2", "content-type": "application/json" },
      body: JSON.stringify({ tenant_id: "tenant_1", incident_type: "high_temp" }),
    });

    await POST(req, { params: Promise.resolve({ path: ["incidents", "store"] }) });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      tenant_id: "tenant_2",
      incident_type: "high_temp",
    });
  });

  it("never adds tenant_id to a JSON body that didn't already carry it", async () => {
    const fetchMock = stubUpstreamFetch();
    const req = new NextRequest("http://localhost/api/bff/some/route", {
      method: "POST",
      headers: { "x-tenant-id": "tenant_2", "content-type": "application/json" },
      body: JSON.stringify({ foo: "bar" }),
    });

    await POST(req, { params: Promise.resolve({ path: ["some", "route"] }) });

    const [, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ foo: "bar" });
  });

  it("leaves a non-JSON body (e.g. multipart upload) streamed through untouched", async () => {
    const fetchMock = stubUpstreamFetch();
    const formData = new FormData();
    formData.set("file", new Blob(["a,b,c"], { type: "text/csv" }), "data.csv");
    const req = new NextRequest("http://localhost/api/bff/upload", {
      method: "POST",
      headers: { "x-tenant-id": "tenant_2" },
      body: formData,
    });

    await POST(req, { params: Promise.resolve({ path: ["upload"] }) });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    // Body is passed through as the original stream/object, not re-encoded
    // as a JSON string — this route must never have parsed it as JSON.
    expect(typeof init.body).not.toBe("string");
  });

  it("401s before ever calling fetch when the session tenant header is missing", async () => {
    const fetchMock = stubUpstreamFetch();
    const req = new NextRequest("http://localhost/api/bff/readings/window?tenant_id=tenant_1");

    const res = await GET(req, { params: Promise.resolve({ path: ["readings", "window"] }) });

    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
