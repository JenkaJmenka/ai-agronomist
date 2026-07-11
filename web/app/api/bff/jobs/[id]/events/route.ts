import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

/**
 * SSE relay for job progress (plan §6.1, G5): pipes the FastAPI
 * text/event-stream through the BFF so the API key never reaches the
 * browser. Lives outside the generic catch-all so streaming headers are
 * explicit and never buffered.
 */
const API_URL = process.env.API_URL ?? "http://localhost:8000";
const API_KEY = process.env.API_KEY;

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function GET(req: NextRequest, ctx: RouteContext): Promise<Response> {
  const { id } = await ctx.params;
  const target = new URL(`/api/jobs/${encodeURIComponent(id)}/events`, API_URL);

  const headers = new Headers({ accept: "text/event-stream" });
  if (API_KEY) headers.set("X-API-Key", API_KEY);

  try {
    const upstream = await fetch(target, {
      headers,
      cache: "no-store",
      signal: req.signal,
    });
    if (!upstream.ok || !upstream.body) {
      return NextResponse.json(
        { detail: `Job events unavailable (${upstream.status})` },
        { status: upstream.status || 502 },
      );
    }
    return new Response(upstream.body, {
      status: 200,
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-store",
        connection: "keep-alive",
        "x-accel-buffering": "no",
      },
    });
  } catch {
    return NextResponse.json({ detail: "Upstream API unreachable" }, { status: 502 });
  }
}
