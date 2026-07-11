"""API-key validation endpoint for the Next.js BFF (migration plan G7)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header

from src.api.auth import resolve_tenant_from_api_key
from src.config import AUTH_ENABLED

router = APIRouter(prefix="/api/auth", tags=["Auth"])


@router.get("/validate")
async def validate_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> dict[str, object]:
    """Report whether the presented X-API-Key is valid and its tenant scope.

    Always answers 200 so the BFF can branch on the body instead of
    exception-handling a 401: `{ok: false}` for unknown/missing keys,
    `{ok: true, tenant_scope: <tenant>}` for valid ones. With auth
    disabled every key is acceptable and there is no tenant scope.
    """
    if not AUTH_ENABLED:
        return {"ok": True, "tenant_scope": None}
    tenant_id = resolve_tenant_from_api_key(x_api_key)
    if tenant_id is None:
        return {"ok": False, "tenant_scope": None}
    return {"ok": True, "tenant_scope": tenant_id}
