/**
 * Client-side PROFILE helpers. Timings POST to `/api/profile` so they land
 * in Railway `web` logs (gated server-side by PROFILE_LOGGING_ENABLED).
 */

export type ProfileFields = Record<string, string | number | boolean>;

let cachedEnabled: boolean | null = null;
let enabledInflight: Promise<boolean> | null = null;

/** Reset cached flag (tests only). */
export function resetProfileEnabledCache(): void {
  cachedEnabled = null;
  enabledInflight = null;
}

export async function isProfileEnabled(): Promise<boolean> {
  if (cachedEnabled !== null) return cachedEnabled;
  if (enabledInflight) return enabledInflight;

  enabledInflight = fetch("/api/profile")
    .then(async (res) => {
      if (!res.ok) return false;
      const body: unknown = await res.json();
      if (typeof body !== "object" || body === null || !("enabled" in body)) return false;
      return Boolean((body as { enabled: unknown }).enabled);
    })
    .catch(() => false)
    .then((enabled) => {
      cachedEnabled = enabled;
      return enabled;
    });

  return enabledInflight;
}

async function postProfile(
  phase: string,
  ok: boolean,
  elapsedMs: number | null,
  fields: ProfileFields,
): Promise<void> {
  if (!(await isProfileEnabled())) return;

  const payload: Record<string, string | number | boolean> = {
    phase,
    ok,
    ...fields,
  };
  if (elapsedMs != null) payload.elapsed_ms = Number(elapsedMs.toFixed(1));

  try {
    await fetch("/api/profile", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    });
  } catch {
    // Fire-and-forget — never break UI on profiling failures.
  }
}

/** Point-in-time or timed client PROFILE event. */
export function profileEvent(
  phase: string,
  options: { ok?: boolean; elapsed_ms?: number } & ProfileFields = {},
): void {
  const { ok = true, elapsed_ms, ...fields } = options;
  void postProfile(phase, ok, elapsed_ms ?? null, fields);
}

/**
 * Time an async/sync block and POST a PROFILE line on completion.
 * Errors are rethrown after emitting ok=0.
 */
export async function timed<T>(
  phase: string,
  fn: () => T | Promise<T>,
  fields: ProfileFields = {},
): Promise<T> {
  const start = performance.now();
  let ok = true;
  try {
    return await fn();
  } catch (err) {
    ok = false;
    throw err;
  } finally {
    await postProfile(phase, ok, performance.now() - start, fields);
  }
}

/**
 * Measure wall time from call until after the next two animation frames
 * (approximate interaction → paint). Use for sync UI updates that freeze
 * the main thread during render.
 *
 * Never starts the enabled-probe on this path — Providers warms the cache.
 * If still unknown, warm in the background and skip this sample.
 */
export function measureInteraction(phase: string, fields: ProfileFields = {}): void {
  const start = performance.now();
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (cachedEnabled === null) {
        void isProfileEnabled();
        return;
      }
      if (!cachedEnabled) return;
      void postProfile(phase, true, performance.now() - start, fields);
    });
  });
}
