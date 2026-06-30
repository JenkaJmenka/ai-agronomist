"""Shared HTTP helpers for dashboard → FastAPI calls."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests


def api_key_from_session() -> Optional[str]:
    """Read API key from Streamlit session when available."""
    try:
        import streamlit as st

        return st.session_state.get("api_key") or None
    except Exception:
        return None


def build_api_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    """Headers for authenticated API requests (X-API-Key when configured)."""
    key = api_key if api_key is not None else api_key_from_session()
    if not key:
        key = os.getenv("API_KEY")
    headers: Dict[str, str] = {}
    if key:
        headers["X-API-Key"] = key
    return headers


def raise_for_status_with_detail(resp: requests.Response) -> None:
    """Like ``raise_for_status`` but appends JSON/text body to the error."""
    if resp.ok:
        return
    detail = resp.text.strip()
    if detail:
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} for {resp.url}: {detail[:500]}",
            response=resp,
        )
    resp.raise_for_status()


def api_get(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    timeout: int = 30,
) -> requests.Response:
    resp = requests.get(
        url,
        params=params,
        headers=build_api_headers(api_key),
        timeout=timeout,
    )
    raise_for_status_with_detail(resp)
    return resp


def api_post(
    url: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    timeout: int = 120,
) -> requests.Response:
    resp = requests.post(
        url,
        json=json,
        data=data,
        files=files,
        headers=build_api_headers(api_key),
        timeout=timeout,
    )
    raise_for_status_with_detail(resp)
    return resp


def api_delete(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    timeout: int = 30,
) -> requests.Response:
    resp = requests.delete(
        url,
        params=params,
        headers=build_api_headers(api_key),
        timeout=timeout,
    )
    raise_for_status_with_detail(resp)
    return resp
