/**
 * Classifies a BFF fetch error into a diagnosable failure category.
 *
 * BFF helpers throw errors whose messages follow the pattern
 * "... failed (STATUS)" (e.g. "Anomaly count request failed (422)"). This
 * utility parses the embedded HTTP status and returns the failure class so
 * error UIs can surface actionable copy rather than generic advice that may be
 * misleading (e.g. "narrow the window" is never correct for a 422).
 */

export type FetchErrorClass = "validation" | "timeout" | "server" | "network";

/**
 * Maps a BFF fetch error to its failure class:
 *
 * - `"validation"` — HTTP 422 or other 4xx: request params violated the API
 *    contract. Narrowing the window / retrying will not help — this needs a
 *    code fix.
 * - `"timeout"`    — HTTP 408 or 504: the server stopped waiting.
 * - `"server"`     — HTTP 5xx (non-timeout): transient failure, retry may help.
 * - `"network"`    — No HTTP status in message: connectivity or CORS failure.
 */
export function classifyFetchError(error: Error | null): FetchErrorClass {
  if (error === null) return "network";
  const match = /\((\d{3})\)/.exec(error.message);
  if (!match) return "network";
  const status = Number(match[1]);
  if (status === 422 || (status >= 400 && status < 500)) return "validation";
  if (status === 408 || status === 504) return "timeout";
  if (status >= 500) return "server";
  return "network";
}

const FAILURE_HINT: Readonly<Record<FetchErrorClass, string>> = {
  validation: "invalid request params — this is a bug, please report it",
  timeout: "request timed out — retry",
  server: "server error — retry or contact support",
  network: "network error — check connectivity and retry",
};

/**
 * Returns a short parenthetical hint for embedding in error messages, e.g.:
 * `"Could not load anomaly count (network error — check connectivity and retry)."`
 */
export function fetchErrorHint(error: Error | null): string {
  return FAILURE_HINT[classifyFetchError(error)];
}
