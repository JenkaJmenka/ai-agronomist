"""Disk cache for cold-run upload inference results (survives page reload)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_CACHE_DIRNAME = "upload_inference_cache"


def cache_root() -> Path:
    base = Path(os.environ.get("DASHBOARD_DATA_DIR", "data"))
    return base / _CACHE_DIRNAME


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", str(value).strip())
    return cleaned or "unknown"


def cache_path(tenant_id: str, greenhouse_id: str) -> Path:
    name = f"{_safe_segment(tenant_id)}__{_safe_segment(greenhouse_id)}.json"
    return cache_root() / name


def slim_results_for_cache(results: dict[str, Any]) -> dict[str, Any]:
    """Drop bulky fields not needed for charts/tables."""
    slim = dict(results)
    slim.pop("embeddings", None)
    slim.pop("uncertainties", None)
    return slim


def build_cache_payload(
    results: dict[str, Any],
    tenant_id: str,
    greenhouse_id: str,
    *,
    filename: str | None = None,
) -> dict[str, Any]:
    return {
        "results": slim_results_for_cache(results),
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "filename": filename,
    }


def write_upload_cache(payload: dict[str, Any]) -> None:
    tenant_id = str(payload.get("tenant_id", ""))
    greenhouse_id = str(payload.get("greenhouse_id", ""))
    if not tenant_id or not greenhouse_id:
        return
    path = cache_path(tenant_id, greenhouse_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_upload_cache(tenant_id: str, greenhouse_id: str) -> dict[str, Any] | None:
    path = cache_path(tenant_id, greenhouse_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "results" not in data:
        return None
    return data


def delete_upload_cache(tenant_id: str, greenhouse_id: str) -> None:
    try:
        cache_path(tenant_id, greenhouse_id).unlink(missing_ok=True)
    except OSError:
        pass
