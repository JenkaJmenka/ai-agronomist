import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST as postUpload } from "@/app/api/bff/inference/upload/route";
import { POST as postUploadAsync } from "@/app/api/bff/inference/upload-async/route";

/**
 * Regression coverage for the two dedicated CSV upload routes (security
 * phase 2 follow-up). They exist because `proxy.ts` excludes their paths
 * from its matcher — Next 16's proxy buffers every matched body with a hard
 * 10MB cap and silently truncates past it — which means two contracts have
 * to hold here and nowhere else:
 *
 *   1. the route resolves the session itself and fails closed, since the
 *      proxy's auth gate and `x-tenant-id` stamp never ran for these paths;
 *   2. the request body is streamed straight through, never read into
 *      memory — buffering it here would reintroduce the exact truncation
 *      the matcher exclusion was added to avoid.
 *
 * Assertions are on what reaches the mocked upstream `fetch`, not on route
 * internals, so they survive a refactor of how the handler is composed.
 */

interface SessionStub {
  readonly user?: { readonly tenantId?: string };
}

const authMock = vi.hoisted(() => vi.fn<() => Promise<SessionStub | null>>());

vi.mock("@/auth", () => ({ auth: authMock }));

/** Node's fetch accepts `duplex`, which the DOM `RequestInit` type omits. */
type UpstreamFetchInit = RequestInit & { readonly duplex?: string };

const SESSION_TENANT = "tenant_7";
const UPLOAD_ROUTES = [
  {
    routePath: "/api/bff/inference/upload",
    handlePost: postUpload,
    upstreamPathname: "/api/inference/upload",
  },
  {
    routePath: "/api/bff/inference/upload-async",
    handlePost: postUploadAsync,
    upstreamPathname: "/api/inference/upload-async",
  },
] as const;

function stubUpstreamFetch(response = new Response("{}", { status: 200 })) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function upstreamCall(fetchMock: ReturnType<typeof stubUpstreamFetch>) {
  const [target, init] = fetchMock.mock.calls[0] as [URL, UpstreamFetchInit];
  return { target: new URL(target.toString()), init };
}

function buildCsvUploadRequest(routePath: string, tenantIdHeader?: string): NextRequest {
  const body = new FormData();
  body.set("file", new Blob(["timestamp,temp\n2026-01-01T00:00:00Z,21.5\n"]), "readings.csv");
  body.set("tenant_id", "tenant_from_the_client");
  return new NextRequest(`http://localhost${routePath}`, {
    method: "POST",
    headers: tenantIdHeader ? { "x-tenant-id": tenantIdHeader } : undefined,
    body,
  });
}

function spyOnBodyReaders(req: NextRequest) {
  return {
    formData: vi.spyOn(req, "formData"),
    text: vi.spyOn(req, "text"),
    json: vi.spyOn(req, "json"),
    arrayBuffer: vi.spyOn(req, "arrayBuffer"),
    blob: vi.spyOn(req, "blob"),
  };
}

async function countStreamBytes(stream: ReadableStream<Uint8Array>): Promise<number> {
  const reader = stream.getReader();
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
  }
  return total;
}

beforeEach(() => {
  authMock.mockResolvedValue({ user: { tenantId: SESSION_TENANT } });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe.each(UPLOAD_ROUTES)("$routePath", ({ routePath, handlePost, upstreamPathname }) => {
  it("should 401 when there is no session", async () => {
    authMock.mockResolvedValue(null);
    const fetchMock = stubUpstreamFetch();

    const res = await handlePost(buildCsvUploadRequest(routePath));

    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("should 401 when the session carries no tenant", async () => {
    authMock.mockResolvedValue({ user: {} });
    const fetchMock = stubUpstreamFetch();

    const res = await handlePost(buildCsvUploadRequest(routePath));

    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("should 401 without a session even when the caller sends an x-tenant-id header", async () => {
    authMock.mockResolvedValue(null);
    const fetchMock = stubUpstreamFetch();

    const res = await handlePost(buildCsvUploadRequest(routePath, "tenant_2"));

    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("should never read the request body before forwarding it", async () => {
    const fetchMock = stubUpstreamFetch();
    const req = buildCsvUploadRequest(routePath);
    const bodyReaders = spyOnBodyReaders(req);

    await handlePost(req);

    expect(bodyReaders.formData).not.toHaveBeenCalled();
    expect(bodyReaders.text).not.toHaveBeenCalled();
    expect(bodyReaders.json).not.toHaveBeenCalled();
    expect(bodyReaders.arrayBuffer).not.toHaveBeenCalled();
    expect(bodyReaders.blob).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("should forward the untouched request stream with duplex half", async () => {
    const fetchMock = stubUpstreamFetch();
    const req = buildCsvUploadRequest(routePath);
    const requestStream = req.body;

    await handlePost(req);

    const { init } = upstreamCall(fetchMock);
    expect(init.body).toBe(requestStream);
    expect(init.body).toBeInstanceOf(ReadableStream);
    expect(init.duplex).toBe("half");
  });

  it("should forward a body larger than the proxy's 10MB buffer cap byte for byte", async () => {
    const OVERSIZED_BODY_BYTES = 11 * 1024 * 1024;
    const fetchMock = stubUpstreamFetch();
    const req = new NextRequest(`http://localhost${routePath}`, {
      method: "POST",
      headers: { "content-type": "text/csv" },
      body: new Uint8Array(OVERSIZED_BODY_BYTES).fill(97),
    });

    await handlePost(req);

    const { init } = upstreamCall(fetchMock);
    const forwardedBytes = await countStreamBytes(init.body as ReadableStream<Uint8Array>);
    expect(forwardedBytes).toBe(OVERSIZED_BODY_BYTES);
  });

  it("should stamp the session tenant on the upstream request", async () => {
    const fetchMock = stubUpstreamFetch();

    await handlePost(buildCsvUploadRequest(routePath));

    const { init } = upstreamCall(fetchMock);
    expect((init.headers as Headers).get("X-Tenant-Id")).toBe(SESSION_TENANT);
  });

  it("should ignore a client-supplied x-tenant-id header", async () => {
    const fetchMock = stubUpstreamFetch();

    await handlePost(buildCsvUploadRequest(routePath, "tenant_2"));

    const { init } = upstreamCall(fetchMock);
    expect((init.headers as Headers).get("X-Tenant-Id")).toBe(SESSION_TENANT);
  });

  it("should forward the multipart content-type with its boundary intact", async () => {
    const fetchMock = stubUpstreamFetch();
    const req = buildCsvUploadRequest(routePath);
    const requestContentType = req.headers.get("content-type");

    await handlePost(req);

    const { init } = upstreamCall(fetchMock);
    expect(requestContentType).toMatch(/^multipart\/form-data; boundary=/);
    expect((init.headers as Headers).get("content-type")).toBe(requestContentType);
  });

  it("should POST to its own dedicated upstream path", async () => {
    const fetchMock = stubUpstreamFetch();

    await handlePost(buildCsvUploadRequest(routePath));

    const { target, init } = upstreamCall(fetchMock);
    expect(target.pathname).toBe(upstreamPathname);
    expect(init.method).toBe("POST");
  });

  it("should relay the upstream status and body to the caller", async () => {
    stubUpstreamFetch(
      new Response(JSON.stringify({ detail: "bad csv" }), {
        status: 422,
        headers: { "content-type": "application/json" },
      }),
    );

    const res = await handlePost(buildCsvUploadRequest(routePath));

    expect(res.status).toBe(422);
    await expect(res.json()).resolves.toEqual({ detail: "bad csv" });
  });
});
