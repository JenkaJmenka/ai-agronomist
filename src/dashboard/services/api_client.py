"""
Thin HTTP client for dashboard → FastAPI integration.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from src.dashboard.services.http_utils import raise_for_status_with_detail

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
        raise_for_status_with_detail(resp)
        return resp.json()

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        resp = requests.post(
            f"{self.base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=60,
        )
        raise_for_status_with_detail(resp)
        return resp.json()

    def get_readings_window(
        self,
        tenant_id: str,
        greenhouse_id: str,
        start_iso: str,
        end_iso: str,
    ) -> List[Dict[str, Any]]:
        payload = self._get(
            "/api/readings/window",
            params={
                "tenant_id": tenant_id,
                "greenhouse_id": greenhouse_id,
                "start": start_iso,
                "end": end_iso,
            },
        )
        return payload["rows"]

    def get_readings_recent(
        self,
        tenant_id: str,
        greenhouse_id: str,
        hours: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
        if hours is not None:
            params["hours"] = hours
        return self._get("/api/readings/recent", params=params)

    def get_readings_bounds(
        self, tenant_id: str, greenhouse_id: str
    ) -> Dict[str, Any]:
        return self._get(
            "/api/readings/bounds",
            params={"tenant_id": tenant_id, "greenhouse_id": greenhouse_id},
        )

    def get_anomaly_readings(
        self, tenant_id: str, greenhouse_id: str, hours_back: int
    ) -> List[Dict[str, Any]]:
        payload = self._get(
            "/api/readings/anomalies",
            params={
                "tenant_id": tenant_id,
                "greenhouse_id": greenhouse_id,
                "hours_back": hours_back,
            },
        )
        return payload["rows"]

    def get_raw_readings(
        self, tenant_id: str, greenhouse_id: str, hours_back: int
    ) -> List[Dict[str, Any]]:
        payload = self._get(
            "/api/readings/raw",
            params={
                "tenant_id": tenant_id,
                "greenhouse_id": greenhouse_id,
                "hours_back": hours_back,
            },
        )
        return payload["rows"]

    def raw_readings_exist(
        self,
        tenant_id: str,
        greenhouse_id: str,
        hours_back: Optional[int] = None,
    ) -> bool:
        params: Dict[str, Any] = {"tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
        if hours_back is not None:
            params["hours_back"] = hours_back
        return self._get("/api/readings/raw/exists", params=params)["exists"]

    def get_greenhouses(self, tenant_id: str) -> List[str]:
        return self._get(
            "/api/readings/greenhouses", params={"tenant_id": tenant_id}
        )["greenhouses"]

    def get_incidents(
        self,
        tenant_id: str,
        greenhouse_id: str,
        start_iso: Optional[str] = None,
        end_iso: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
        if start_iso:
            params["start"] = start_iso
        if end_iso:
            params["end"] = end_iso
        return self._get("/api/readings/incidents", params=params)["rows"]

    def upload_csv_async(
        self,
        *,
        tenant_id: str,
        greenhouse_id: str,
        file_name: str,
        file_bytes: bytes,
        persist_data: bool = True,
        train_model: bool = False,
    ) -> Dict[str, Any]:
        """Queue a CSV for portioned processing; returns {job_id, portion_count, poll_url}."""
        resp = requests.post(
            f"{self.base_url}/api/inference/upload-async",
            files={"file": (file_name, file_bytes, "text/csv")},
            data={
                "tenant_id": tenant_id,
                "greenhouse_id": greenhouse_id,
                "persist_data": str(persist_data).lower(),
                "train_model": str(train_model).lower(),
            },
            headers={k: v for k, v in self._headers().items() if k != "Content-Type"},
            timeout=(
                int(os.getenv("UPLOAD_QUEUE_CONNECT_TIMEOUT", "30")),
                int(os.getenv("UPLOAD_QUEUE_TIMEOUT", "300")),
            ),
        )
        raise_for_status_with_detail(resp)
        return resp.json()

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Poll async job status/progress/result."""
        return self._get(f"/api/jobs/{job_id}")

    def list_jobs(
        self,
        tenant_id: str,
        *,
        greenhouse_id: Optional[str] = None,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Jobs for a tenant, newest first."""
        params: Dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
        if greenhouse_id:
            params["greenhouse_id"] = greenhouse_id
        if job_type:
            params["job_type"] = job_type
        if status:
            params["status"] = status
        return self._get("/api/jobs", params=params)["jobs"]

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

    def get_active_model_version(self, tenant_id: str) -> str:
        """Return active model_id for tenant, or 'baseline' if none deployed."""
        try:
            r = self._get(f"/api/learning/models/{tenant_id}/active-version")
            return r.get("model_id", "baseline")
        except Exception:
            return "baseline"

    def list_model_versions(self, tenant_id: str) -> List[Dict[str, Any]]:
        return self._get(f"/api/models/{tenant_id}/versions")

    def activate_model(self, tenant_id: str, model_id: str) -> Dict[str, Any]:
        return self._post(
            f"/api/models/{tenant_id}/activate",
            {"model_id": model_id},
        )

    def delete_model(self, tenant_id: str, model_id: str, *, force: bool = False) -> Dict[str, Any]:
        resp = requests.delete(
            f"{self.base_url}/api/models/{tenant_id}/versions/{model_id}",
            params={"force": str(force).lower()},
            headers=self._headers(),
            timeout=30,
        )
        raise_for_status_with_detail(resp)
        return resp.json()

    def clear_models(self, tenant_id: str) -> Dict[str, Any]:
        resp = requests.delete(
            f"{self.base_url}/api/models/{tenant_id}/versions",
            headers=self._headers(),
            timeout=30,
        )
        raise_for_status_with_detail(resp)
        return resp.json()
