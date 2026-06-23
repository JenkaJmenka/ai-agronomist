"""
Thin HTTP client for dashboard → FastAPI integration.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

DEFAULT_API_URL = "http://localhost:8000"


class DashboardApiClient:
    """Sync requests wrapper for operator dashboard pages."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("API_URL", DEFAULT_API_URL)).rstrip(
            "/"
        )
        self.api_key = api_key or os.getenv("API_KEY")

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = requests.get(
            f"{self.base_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        resp = requests.post(
            f"{self.base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def mark_anomaly(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/api/feedback/mark-anomaly", payload)

    def learning_progress(self, customer_id: str) -> Dict[str, Any]:
        return self._get(f"/api/feedback/learning-progress/{customer_id}")

    def statistics(self, customer_id: str) -> Dict[str, Any]:
        return self._get(f"/api/feedback/statistics/{customer_id}")

    def list_open_incidents(
        self, tenant_id: str, greenhouse_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"tenant_id": tenant_id}
        if greenhouse_id:
            params["greenhouse_id"] = greenhouse_id
        return self._get("/api/incidents/open", params=params)

    def get_incident(self, incident_id: str, tenant_id: str) -> Dict[str, Any]:
        return self._get(
            f"/api/incidents/{incident_id}",
            params={"tenant_id": tenant_id},
        )

    def comprehensive_inference(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/api/inference/comprehensive", payload)

    def get_monitoring_metrics(self, hours: int = 24) -> Dict[str, Any]:
        return self._get("/api/monitoring/metrics", params={"hours": hours})
