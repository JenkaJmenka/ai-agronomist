"""
API key authentication for tenant-scoped endpoints.

When ``AUTH_ENABLED`` is True, clients must send ``X-API-Key`` matching a
tenant entry in ``TENANT_API_KEYS``. The authenticated tenant must match the
``customer_id`` / ``tenant_id`` on the request (403 on mismatch).
"""

import secrets
from typing import Dict, Optional

from fastapi import Header, HTTPException, status

from src.config import AUTH_ENABLED, TENANT_API_KEYS


def resolve_tenant_from_api_key(api_key: Optional[str]) -> Optional[str]:
    """Map an API key to its tenant id, or None if unknown."""
    if not api_key:
        return None
    for tenant_id, expected_key in TENANT_API_KEYS.items():
        if secrets.compare_digest(api_key, expected_key):
            return tenant_id
    return None


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> Optional[str]:
    """FastAPI dependency: returns authenticated tenant_id or None if auth off."""
    if not AUTH_ENABLED:
        return None
    tenant_id = resolve_tenant_from_api_key(x_api_key)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return tenant_id


def enforce_tenant_access(
    auth_tenant: Optional[str], requested_tenant: str
) -> None:
    """Raise 403 when authenticated tenant does not match the request."""
    if auth_tenant is None:
        return
    if auth_tenant != requested_tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant access denied",
        )


def register_test_api_key(tenant_id: str, api_key: str) -> None:
    """Test helper: register a tenant key at runtime."""
    TENANT_API_KEYS[tenant_id] = api_key


def clear_test_api_keys() -> None:
    """Test helper: reset tenant keys to env-loaded values."""
    TENANT_API_KEYS.clear()
    TENANT_API_KEYS.update(_load_keys_from_env())


def _load_keys_from_env() -> Dict[str, str]:
    from src.config import _parse_tenant_api_keys

    return _parse_tenant_api_keys()
