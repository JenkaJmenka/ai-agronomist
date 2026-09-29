import { type NextRequest, NextResponse } from "next/server";

import { overwriteTenantIdInJsonBody } from "@/lib/auth/tenantBodyRewrite";
import { emitProfileLog } from "@/lib/profileLog";

/**
 * BFF proxy (plan §3, §5-G8): the browser only ever talks to this handler.
 * The FastAPI base URL and API key stay server-side.
 */
const API_URL = process.env.API_URL ?? "http://localhost:8000";
const API_KEY = process.env.API_KEY;

const PASSTHROUGH_HEADERS = ["content-type", "accept"] as const;

async function proxy(req: NextRequest, path: string[]): Promise<NextResponse> {
  // proxy.ts stamps this from the session before any request reaches here;
  // a request that arrives without it slipped past auth somehow — 401
  // rather than ever forwarding it upstream unscoped.
  const tenantId = req.headers.get("x-tenant-id");
  if (!tenantId) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }

  const target = new URL(`/api/${path.join("/")}`, API_URL);
  target.search = req.nextUrl.search;
  // The client has no business choosing a tenant — overwrite the session's
  // tenant in, never trust what it sent, and never reject a mismatch (the
  // API does that; the FE's own `useScope()` still defaults to a fixed
  // tenant, so a mismatch here is routine, not an attack). Only touch the
  // param on routes that already carry one — adding it to a route that
  // never expected it risks an "unknown query param" rejection upstream
  // that this route never used to produce.
  if (target.searchParams.has("tenant_id")) {
    target.searchParams.set("tenant_id", tenantId);
  }

  const pathJoined = path.join("/");
  const start = performance.now();

  const headers = new Headers();
  for (const name of PASSTHROUGH_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (API_KEY) headers.set("X-API-Key", API_KEY);
  headers.set("X-Tenant-Id", tenantId);

  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const isJsonBody =
    hasBody && (req.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json");

  let body: BodyInit | undefined;
  let duplex: { duplex: "half" } | Record<string, never> = {};
  if (isJsonBody) {
    // JSON bodies are small enough to buffer, and buffering is the only way
    // to rewrite a `tenant_id` field the client may have sent — the same
    // overwrite-don't-reject rule as the query param above. Every other
    // content type (multipart uploads, binary, none) is left as a streamed
    // pass-through exactly as before; this branch never touches those.
    body = overwriteTenantIdInJsonBody(await req.text(), tenantId);
  } else if (hasBody) {
    body = req.body ?? undefined;
    // Streamed request bodies require duplex on Node fetch.
    if (req.body) duplex = { duplex: "half" as const };
  }

  let status = 502;
  let ok = true;
  try {
    const upstream = await fetch(target, {
      method: req.method,
      headers,
      body,
      ...duplex,
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
    emitProfileLog("bff.proxy", ok, performance.now() - start, {
      method: req.method,
      path: pathJoined,
      status,
    });
  }
}

interface RouteContext {
  params: Promise<{ path: string[] }>;
}

export async function GET(req: NextRequest, ctx: RouteContext) {
  return proxy(req, (await ctx.params).path);
}

export async function POST(req: NextRequest, ctx: RouteContext) {
  return proxy(req, (await ctx.params).path);
}

export async function PUT(req: NextRequest, ctx: RouteContext) {
  return proxy(req, (await ctx.params).path);
}

export async function DELETE(req: NextRequest, ctx: RouteContext) {
  return proxy(req, (await ctx.params).path);
}
