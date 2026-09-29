import { createUploadPostHandler } from "@/app/api/bff/_lib/uploadRouteHandler";

// Persist/train path (`uploadCsvAsync`) — see `_lib/uploadRouteHandler.ts`
// for why this is a dedicated route instead of the `[...path]` catch-all.
export const POST = createUploadPostHandler("inference/upload-async", "bff.upload.async");
