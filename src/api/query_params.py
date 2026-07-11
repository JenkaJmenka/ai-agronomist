"""Shared query-parameter parsing helpers for API routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException


def parse_iso_param(value: str, param: str) -> datetime:
    """Parse an ISO-8601 query value ('Z' suffix allowed) or raise 400."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid ISO timestamp for {param!r}: {value!r}"
        ) from exc
