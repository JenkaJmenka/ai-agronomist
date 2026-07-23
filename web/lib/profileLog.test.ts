import { afterEach, describe, expect, it, vi } from "vitest";

import { emitProfileLog } from "./profileLog";

describe("emitProfileLog", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("is a no-op when PROFILE_LOGGING_ENABLED is off", () => {
    vi.stubEnv("PROFILE_LOGGING_ENABLED", "no");
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    emitProfileLog("bff.proxy", true, 12.3, { path: "readings/recent" });
    expect(info).not.toHaveBeenCalled();
  });

  it("emits a greppable PROFILE line when enabled", () => {
    vi.stubEnv("PROFILE_LOGGING_ENABLED", "yes");
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    emitProfileLog("bff.proxy", true, 12.34, {
      method: "GET",
      path: "readings/recent",
      status: 200,
    });
    expect(info).toHaveBeenCalledTimes(1);
    expect(info.mock.calls[0]?.[0]).toBe(
      "PROFILE phase=bff.proxy ok=1 elapsed_ms=12.3 method=GET path=readings/recent status=200",
    );
  });
});
