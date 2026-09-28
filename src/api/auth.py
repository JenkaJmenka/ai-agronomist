"""API key authentication for tenant-scoped endpoints.

When ``AUTH_ENABLED`` is True, clients must send ``X-API-Key`` matching an
issued credential (see ``src.config.ApiCredential``). Every credential is
one of two caller types:

* ``web`` — the BFF's single server-side credential. It may be scoped to
  every tenant (``tenant_ids == "*"``) and derives the tenant to act as,
  per request, from the ``X-Tenant-Id`` header (set by the BFF from the
  user's session — never trusted from the browser directly).
* ``integration`` — a named external caller (e.g. a SCADA push client)
  bound to exactly one tenant. ``X-Tenant-Id`` is never honoured for this
  caller type; sending one is rejected outright rather than silently
  ignored, so a misconfigured integration client fails loudly.

``require_api_key`` resolves the caller's effective tenant id (or raises).
``enforce_tenant_access`` then checks that tenant against whatever
``tenant_id`` a route/body/query also carries, so a caller can never widen
its own scope by naming a different tenant in the request.

Both fail closed: an unauthenticated or misauthenticated caller can only
ever receive an HTTP error, never a resolved tenant that happens to be
``None`` and gets treated as "any tenant is fine".
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from fastapi import Header, HTTPException, status

from src.config import (
    AUTH_ENABLED,
    ENVIRONMENT,
    RAILWAY_ENVIRONMENT_NAME,
    API_CREDENTIALS,
    ApiCredential,
)


def _hash_key(raw_key: str) -> str:
    """SHA-256 hex digest of a raw API key.

    API keys (unlike passwords) are generated with high entropy, so a fast
    cryptographic hash is the standard choice here — a slow password hash
    (bcrypt/argon2) defends against brute-forcing low-entropy secrets, which
    doesn't apply to a randomly generated key.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _resolve_credential(api_key: Optional[str]) -> Optional[ApiCredential]:
    """Look up the credential matching a presented raw key, or None.

    Compares the presented key's hash against every stored hash in constant
    time (`secrets.compare_digest`) so a mismatch can't be timed to learn
    which prefix of a valid hash it shares.
    """
    if not api_key:
        return None
    presented_hash = _hash_key(api_key)
    for credential in API_CREDENTIALS.values():
        if secrets.compare_digest(presented_hash, credential.key_hash):
            return credential
    return None


def _tenant_for_integration(credential: ApiCredential) -> str:
    """Return the single tenant an integration credential is bound to.

    Enforced at config-parse time (``_build_credential`` in src/config.py)
    that an integration credential has exactly one tenant id — this is a
    defensive re-check, not the primary guard.
    """
    if credential.tenant_ids == "*" or len(credential.tenant_ids) != 1:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Integration credential is not scoped to exactly one tenant",
        )
    return next(iter(credential.tenant_ids))


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> Optional[str]:
    """FastAPI dependency: resolve the caller's effective tenant id.

    Returns ``None`` only when ``AUTH_ENABLED`` is False (local dev / tests
    — refused outright in production by ``assert_auth_configured``, called
    at FastAPI startup). Whenever auth is enabled, this function can only
    ever return a real tenant id or raise an HTTPException — never a
    silent "no tenant" pass-through — so a route that reaches
    ``enforce_tenant_access`` with auth on is guaranteed a tenant to check
    against.
    """
    if not AUTH_ENABLED:
        return None

    credential = _resolve_credential(x_api_key)
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )

    if credential.caller_type == "integration":
        if x_tenant_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Tenant-Id is not permitted for integration credentials",
            )
        return _tenant_for_integration(credential)

    # caller_type == "web": the tenant comes from the session-derived header,
    # never from the credential alone (the BFF's one credential is typically
    # tenant_ids="*", i.e. authorized to act as any tenant the session names).
    if x_tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-Id header is required for this credential",
        )
    if credential.tenant_ids != "*" and x_tenant_id not in credential.tenant_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant access denied",
        )
    return x_tenant_id


def enforce_tenant_access(auth_tenant: Optional[str], requested_tenant: str) -> None:
    """Raise when the authenticated tenant does not match the request.

    When ``AUTH_ENABLED`` is False this is a deliberate dev/test no-op —
    production can never reach this branch because ``assert_auth_configured``
    refuses to start the app with auth disabled there. When auth is enabled,
    a ``None`` ``auth_tenant`` reaching this function is a bug (a route
    called it without a working ``require_api_key`` dependency) and fails
    closed with 401 rather than silently granting access, independently of
    the startup check.
    """
    if not AUTH_ENABLED:
        return
    if auth_tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    if auth_tenant != requested_tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant access denied",
        )


def assert_auth_configured() -> None:
    """Refuse to start when running deployed with auth disabled.

    Called once at FastAPI startup (see the ``lifespan`` in
    ``src.api.main``), not per-request — a misconfigured deployment should
    fail immediately and loudly rather than silently serve every tenant-
    scoped route to anyone.

    Two independent signals, either one sufficient to require
    ``AUTH_ENABLED=true``:

    * ``ENVIRONMENT`` defaults to ``"production"`` (not ``"development"``) —
      a deployment that forgets to set it fails closed instead of silently
      running with auth disabled. Non-production must be declared
      explicitly (local dev / CI / tests set ``ENVIRONMENT=development`` or
      ``=test`` — see ``tests/conftest.py`` and ``docker-compose.yml``).
    * ``RAILWAY_ENVIRONMENT_NAME`` is set by Railway on every deployed
      service regardless of whether this app's own ``ENVIRONMENT`` var was
      configured for it. Its presence alone — independent of ``ENVIRONMENT``
      — also requires auth, so one forgotten variable can't fail open while
      the other silently fails open too: a real deployment always has
      ``RAILWAY_ENVIRONMENT_NAME`` even if ``ENVIRONMENT`` was never set.
    """
    is_production = ENVIRONMENT == "production"
    is_deployed_on_railway = bool(RAILWAY_ENVIRONMENT_NAME)
    if (is_production or is_deployed_on_railway) and not AUTH_ENABLED:
        raise RuntimeError(
            "Refusing to start with tenant authentication disabled on a "
            f"deployed environment (ENVIRONMENT={ENVIRONMENT!r}, "
            f"RAILWAY_ENVIRONMENT_NAME={RAILWAY_ENVIRONMENT_NAME!r}). "
            "Set AUTH_ENABLED=true, or ENVIRONMENT=development for a "
            "genuine local/dev run."
        )


def register_test_api_key(
    tenant_id: str, api_key: str, *, name: str = "test-integration"
) -> None:
    """Test helper: register a single-tenant ``integration`` credential.

    Matches the pre-Phase-2 ``register_test_api_key(tenant_id, api_key)``
    call shape used across the existing test suite: presenting *api_key*
    authenticates as *tenant_id*, no ``X-Tenant-Id`` header involved.
    """
    key_id = f"test:{tenant_id}:{api_key}"
    API_CREDENTIALS[key_id] = ApiCredential(
        key_hash=_hash_key(api_key),
        caller_type="integration",
        name=name,
        tenant_ids=frozenset({tenant_id}),
    )


def register_test_web_api_key(
    api_key: str, *, tenant_ids: frozenset[str] | str = "*", name: str = "test-web"
) -> None:
    """Test helper: register a ``web`` credential (tenant via X-Tenant-Id)."""
    key_id = f"test-web:{api_key}"
    API_CREDENTIALS[key_id] = ApiCredential(
        key_hash=_hash_key(api_key),
        caller_type="web",
        name=name,
        tenant_ids=tenant_ids,
    )


def clear_test_api_keys() -> None:
    """Test helper: reset credentials to env-loaded values."""
    from src.config import _parse_api_credentials

    API_CREDENTIALS.clear()
    API_CREDENTIALS.update(_parse_api_credentials())
