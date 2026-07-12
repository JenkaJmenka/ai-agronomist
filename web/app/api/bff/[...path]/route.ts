import { type NextRequest, NextResponse } from "next/server";

/**
 * BFF proxy (plan §3, §5-G8): the browser only ever talks to this handler.
 * The FastAPI base URL and API key stay server-side.
 */
const API_URL = process.env.API_URL ?? "http://localhost:8000";
const API_KEY = process.env.API_KEY;

const PASSTHROUGH_HEADERS = ["content-type", "accept"] as const;

async function proxy(req: NextRequest, path: string[]): Promise<NextResponse> {
  const target = new URL(`/api/${path.join("/")}`, API_URL);
  target.search = req.nextUrl.search;

  const headers = new Headers();
  for (const name of PASSTHROUGH_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (API_KEY) headers.set("X-API-Key", API_KEY);

  try {
    const upstream = await fetch(target, {
      method: req.method,
      headers,
      body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
      // Streamed request bodies require duplex on Node fetch.
      ...(req.body ? { duplex: "half" as const } : {}),
      cache: "no-store",
    });
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
      },
    });
  } catch {
    return NextResponse.json({ detail: "Upstream API unreachable" }, { status: 502 });
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
