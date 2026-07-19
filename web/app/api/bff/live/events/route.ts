import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

/**
 * SSE relay for live monitoring (Phase 5A): pipes the FastAPI
 * text/event-stream through the BFF so the API key never reaches the browser.
 * Named events (reading, anomaly, anomaly-detail, source-status, heartbeat,
 * reset) are forwarded verbatim; Last-Event-ID resume is handled automatically
 * by the browser once we forward the header upstream.
 */
const API_URL = process.env.API_URL ?? "http://localhost:8000";
const API_KEY = process.env.API_KEY;

export async function GET(req: NextRequest): Promise<Response> {
  const tenant = req.nextUrl.searchParams.get("tenant") ?? "";
  const gh = req.nextUrl.searchParams.get("gh") ?? "";

  const target = new URL("/api/live/events", API_URL);
  if (tenant) target.searchParams.set("tenant_id", tenant);
  if (gh) target.searchParams.set("greenhouse_id", gh);

  const headers = new Headers({ accept: "text/event-stream" });
  if (API_KEY) headers.set("X-API-Key", API_KEY);

  // Forward Last-Event-ID so Redis XREAD resumes from the right offset.
  const lastEventId = req.headers.get("last-event-id");
  if (lastEventId) headers.set("last-event-id", lastEventId);

  try {
    const upstream = await fetch(target.toString(), {
      headers,
      cache: "no-store",
      signal: req.signal,
    });

    if (!upstream.ok || !upstream.body) {
      return NextResponse.json(
        { detail: `Live events unavailable (${upstream.status})` },
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
