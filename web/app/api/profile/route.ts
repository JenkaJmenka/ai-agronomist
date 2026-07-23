import { NextResponse } from "next/server";

import { isProfileLoggingEnabled } from "@/lib/profileFlag";
import { emitProfileLog, type ProfileFieldValue } from "@/lib/profileLog";

/**
 * Profile ingest for the browser. Client timings POST here so PROFILE lines
 * appear in Railway `web` logs. Gated by PROFILE_LOGGING_ENABLED (server env).
 */

export async function GET(): Promise<NextResponse> {
  return NextResponse.json({ enabled: isProfileLoggingEnabled() });
}

export async function POST(req: Request): Promise<NextResponse> {
  if (!isProfileLoggingEnabled()) {
    return new NextResponse(null, { status: 204 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ detail: "Invalid JSON" }, { status: 400 });
  }

  if (typeof body !== "object" || body === null) {
    return NextResponse.json({ detail: "Expected object body" }, { status: 400 });
  }

  const record = body as Record<string, unknown>;
  const phase = record.phase;
  if (typeof phase !== "string" || phase.trim() === "") {
    return NextResponse.json({ detail: "phase is required" }, { status: 400 });
  }

  const ok = record.ok === undefined ? true : Boolean(record.ok);
  const elapsedRaw = record.elapsed_ms;
  const elapsedMs =
    typeof elapsedRaw === "number" && Number.isFinite(elapsedRaw) ? elapsedRaw : null;

  const fields: Record<string, ProfileFieldValue> = {};
  for (const [key, value] of Object.entries(record)) {
    if (key === "phase" || key === "ok" || key === "elapsed_ms") continue;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      fields[key] = value;
    }
  }

  emitProfileLog(phase, ok, elapsedMs, fields);
  return new NextResponse(null, { status: 204 });
}
