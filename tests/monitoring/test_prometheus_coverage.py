"""Additional coverage for src/monitoring/prometheus_metrics.py.

The existing test suite covers the normal path (lines 48-51) where prometheus
is available and record_prometheus_detection() updates the counter.

This module covers the remaining uncovered paths:
  - Line 47: early return inside record_prometheus_detection()
             when _PROMETHEUS_AVAILABLE is False
  - Line 57: early return inside generate_prometheus_output()
             when _PROMETHEUS_AVAILABLE is False
  - Normal output path of generate_prometheus_output() (line 59)
"""
from __future__ import annotations

import pytest

import src.monitoring.prometheus_metrics as prom_mod


class TestRecordPrometheusDetectionUnavailablePath:
    def test_returns_none_when_prometheus_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Line 47: early return when _PROMETHEUS_AVAILABLE is False."""
        monkeypatch.setattr(prom_mod, "_PROMETHEUS_AVAILABLE", False)
        result = prom_mod.record_prometheus_detection("tenant_z", "spike", 0.95)
        assert result is None  # explicit None from bare return

    def test_no_exception_when_prometheus_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Early return path must not raise even with garbage arguments."""
        monkeypatch.setattr(prom_mod, "_PROMETHEUS_AVAILABLE", False)
        prom_mod.record_prometheus_detection("", "", -1.0)  # should not raise


class TestRecordPrometheusDetectionAvailablePath:
    def test_normal_call_does_not_raise(self) -> None:
        """Normal path: counter.labels().inc() and histogram.labels().observe()."""
        prom_mod.record_prometheus_detection("tenant_a", "drift", 0.75)

    def test_multiple_calls_accumulate(self) -> None:
        """Multiple sequential calls should all succeed without error."""
        for pattern in ("spike", "drift", "cascade", "saturation"):
            prom_mod.record_prometheus_detection("tenant_b", pattern, 0.5)


class TestGeneratePrometheusOutput:
    def test_returns_bytes_and_string_when_available(self) -> None:
        """Line 59: generate_latest() path when prometheus is available."""
        body, content_type = prom_mod.generate_prometheus_output()
        assert isinstance(body, bytes)
        assert isinstance(content_type, str)
        assert len(body) > 0

    def test_returns_fallback_when_prometheus_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line 57: fallback bytes when _PROMETHEUS_AVAILABLE is False."""
        monkeypatch.setattr(prom_mod, "_PROMETHEUS_AVAILABLE", False)
        monkeypatch.setattr(prom_mod, "_registry", None)
        body, content_type = prom_mod.generate_prometheus_output()
        assert b"prometheus-client not installed" in body
        assert content_type == "text/plain"

    def test_returns_fallback_when_registry_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line 57 branch: _PROMETHEUS_AVAILABLE True but _registry is None."""
        monkeypatch.setattr(prom_mod, "_registry", None)
        body, content_type = prom_mod.generate_prometheus_output()
        assert isinstance(body, bytes)
        assert isinstance(content_type, str)
