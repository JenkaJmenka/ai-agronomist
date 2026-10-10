import { NextResponse } from "next/server";

import { emitProfileLog } from "@/lib/profileLog";

/**
 * Shared upstream-forwarding core for every BFF route that isn't an SSE
 * relay (those stay bespoke — see `jobs/[id]/events` and `live/events`).
 * Extracted from the catch-all proxy (plan §3, §5-G8) so the dedicated
 * upload routes (see `../inference/upload{,-async}/route.ts`, security phase 2
 * follow-up) can reuse the same fetch/error/profiling behavior instead of
 * duplicating it.
 */
const API_URL = process.env.API_URL ?? "http://localhost:8000";
const API_KEY = process.env.API_KEY;

export interface UpstreamForwardRequest {
  readonly method: string;
  /** Path under `/api/`, e.g. "inference/upload" — no leading/trailing slash. */
  readonly apiPath: string;
  /** Already-tenant-scoped query string, e.g. "?gh=gh_A" or "". */
  readonly search?: string;
  readonly tenantId: string;
  /** Additional headers to forward (content-type, accept, ...). */
  readonly headers?: Headers;
  readonly body?: BodyInit | null;
  /** Set when `body` is a streamed `ReadableStream` (Node fetch requires it). */
  readonly duplex?: boolean;
  /** PROFILE phase name, e.g. "bff.proxy" or "bff.upload". */
  readonly profilePhase: string;
}

/** Forwards a request to the FastAPI upstream and relays its response verbatim. */
export async function forwardToUpstream(request: UpstreamForwardRequest): Promise<NextResponse> {
  const target = new URL(`/api/${request.apiPath}`, API_URL);
  if (request.search) target.search = request.search;

  const headers = new Headers(request.headers);
  if (API_KEY) headers.set("X-API-Key", API_KEY);
  headers.set("X-Tenant-Id", request.tenantId);

  const start = performance.now();
  let status = 502;
  let ok = true;
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.body,
      ...(request.duplex ? { duplex: "half" as const } : {}),
      cache: "no-store",
    });
    status = upstream.status;
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
      },
    });
  } catch {
    ok = false;
    return NextResponse.json({ detail: "Upstream API unreachable" }, { status: 502 });
  } finally {
    emitProfileLog(request.profilePhase, ok, performance.now() - start, {
      method: request.method,
      path: request.apiPath,
      status,
    });
  }
}
