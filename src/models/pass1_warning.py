"""The structured warning Pass 1 returns to its callers.

A Pass-1 warning crosses a trust boundary: `BootstrapPass1Result.warnings`
reaches the tenant through `POST /api/v1/bootstrap/run` and is persisted in
`metrics_json`. The previous free-text form leaked our configuration surface
into that output — it told the caller to "add it to
BOOTSTRAP_MAD_EXCLUDED_CHANNELS" — which named an internal environment
variable to a customer and put it in `web/openapi.json`.

So a warning is now two things with two different audiences:

* **`code`** — stable, machine-readable, safe to branch on and to store.
  Drawn from the closed set in `PASS1_WARNING_CODES` below. Consumers should
  match on the module constants, never on the literal strings.
* **`message`** — tenant-safe prose. **No environment variable names, no
  internal constant names, no internal module or field names.** It states what
  happened to the caller's data in the caller's terms.

The operator-facing remediation that used to live in `message` has not been
deleted, it has been **relocated to the log**, where naming
`BOOTSTRAP_MAD_EXCLUDED_CHANNELS` and printing the `mad`/`median` pair is
exactly right. See `mad_scrub._build_warnings` and
`pass1_discard_guard._with_warning`: each logs the full engineering detail at
WARNING level and returns only the tenant-safe half.

Keep the two halves honest about the same event. If you add a code, add it to
`PASS1_WARNING_CODES` in the same edit — `tests/` asserts on the closed set and
the backend serializes it.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# The closed set of warning codes.
# ---------------------------------------------------------------------------

# Union discard reached the review threshold but training proceeded.
# Accompanies discard status "warn"; the result is still returned.
WARNING_CODE_DISCARD_HIGH: str = "discard_fraction_high"

# Union discard reached the hard limit; training was stopped.
# Accompanies discard status "fail", raised as ExcessivePass1DiscardError.
WARNING_CODE_DISCARD_EXCESSIVE: str = "discard_fraction_excessive"

# One sensor channel contributed more than its share of the MAD cull.
# Emitted once per offending channel, so this code can repeat in one result.
WARNING_CODE_CHANNEL_CULL_HIGH: str = "channel_cull_fraction_high"

PASS1_WARNING_CODES: frozenset = frozenset(
    {
        WARNING_CODE_DISCARD_HIGH,
        WARNING_CODE_DISCARD_EXCESSIVE,
        WARNING_CODE_CHANNEL_CULL_HIGH,
    }
)


@dataclass(frozen=True)
class Pass1Warning:
    """One Pass-1 warning: a stable code plus tenant-safe prose.

    ``code`` is always a member of :data:`PASS1_WARNING_CODES`. ``message`` is
    safe to show a customer and safe to put in an HTTP response body.
    """

    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        """Serialization form for ``metrics_json`` and HTTP responses.

        Provided so no caller has to reach into the dataclass or re-derive the
        key names; the API layer serializes with
        ``[warning.as_dict() for warning in result.warnings]``.
        """
        return {"code": self.code, "message": self.message}
