import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { auth } from "@/auth";

import { forwardToUpstream } from "./forwardToUpstream";

/**
 * Factory for the dedicated (non-catch-all) CSV upload routes.
 *
 * `proxy.ts` excludes `/api/bff/inference/upload` and `/api/bff/inference/
 * upload-async` from its matcher: Next 16's proxy buffers every matched
 * request body in memory with a hard 10MB cap and silently truncates
 * anything larger (`proxyClientMaxBodySize` docs) — fatal for CSV uploads,
 * which corrupts the multipart body and the `tenant_id`/`file` fields that
 * follow it. Excluded routes never receive the proxy's `x-tenant-id` stamp,
 * so — unlike every other BFF route — these resolve the session themselves
 * and fail closed before forwarding anything upstream.
 *
 * The request body is streamed through untouched (`duplex: "half"`), never
 * read with `req.formData()`/`req.text()` — buffering it here would
 * reintroduce the same truncation bug this route exists to avoid.
 */
export function createUploadPostHandler(apiPath: string, profilePhase: string) {
  return async function POST(req: NextRequest): Promise<NextResponse> {
    const session = await auth();
    const tenantId = session?.user?.tenantId;
    if (!tenantId) {
      return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
    }

    const headers = new Headers();
    const contentType = req.headers.get("content-type");
    if (contentType) headers.set("content-type", contentType);

    return forwardToUpstream({
      method: "POST",
      apiPath,
      tenantId,
      headers,
      body: req.body,
      duplex: Boolean(req.body),
      profilePhase,
    });
  };
}
