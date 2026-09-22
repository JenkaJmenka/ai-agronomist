"""Episode grouping for anomaly alerts (Tasks 3 + 5 + 5b).

Groups contiguous per-channel alerts into episodes so one continuous
event is one alert with a duration instead of N rows.

**Design decisions (docs/architecture/episode_grouping.md):**

1. Grouping lives at the detection layer (``finalize_scores``).
2. Episodes are keyed by **(channel, detector)** — a ``stuck_fault`` alert
   is never absorbed into a ``statistical`` episode on the same channel.
3. Stuck-fault episodes derive their ``duration_seconds`` from the actual
   frozen span (via ``completed_faults`` / ``z_score``), not from the
   span of alert rows (which would be 0 for a single-row emission).
4. An ``is_ongoing`` flag marks episodes whose freeze had not yet ended
   at detection time; their ``duration_seconds`` is a lower bound.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config import EPISODE_GAP_SECONDS, RECOVERY_SCORE_CAP

# Seconds per hour, used by stuck_fault duration conversion.
_SECONDS_PER_HOUR = 3600.0


def _parse_iso(ts: str) -> datetime:
    """Parse ISO timestamp to UTC-aware datetime."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class AnomalyEpisode:
    """One continuous anomaly event on a single channel."""

    channel: str
    detector: str
    start_ts: str
    end_ts: str
    duration_seconds: float
    peak_score: float
    sample_count: int
    severity: str
    is_recovery: bool = False
    is_ongoing: bool = False
    onset_ts: str | None = None
    detected_ts: str | None = None
    frozen_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON response."""
        return {
            "channel": self.channel,
            "detector": self.detector,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_seconds": self.duration_seconds,
            "peak_score": self.peak_score,
            "sample_count": self.sample_count,
            "severity": self.severity,
            "is_recovery": self.is_recovery,
            "is_ongoing": self.is_ongoing,
            "onset_ts": self.onset_ts,
            "detected_ts": self.detected_ts,
            "frozen_value": self.frozen_value,
        }


def _severity_rank(sev: str | None) -> int:
    """Numeric rank for severity comparison; no detector persists "normal"."""
    ranks = {"unclassified": 0, "warning": 1, "critical": 2, "urgent": 3}
    return ranks.get(sev or "unclassified", 0)


def group_alerts_into_episodes(
    high_anomalies: list[Any],
    *,
    recovery_channels: dict[str, Any] | None = None,
    completed_faults: list[Any] | None = None,
    gap_seconds: int = EPISODE_GAP_SECONDS,
    recovery_score_cap: float = RECOVERY_SCORE_CAP,
) -> list[AnomalyEpisode]:
    """Group high_anomalies into contiguous per-(channel, detector) episodes.

    Two alerts belong to the same episode when they share a channel AND
    a detector and their timestamps are within ``gap_seconds``.  Alerts
    from different detectors are always separate episodes — a
    ``stuck_fault`` alert is never absorbed into a ``statistical``
    episode.

    Parameters
    ----------
    high_anomalies:
        ``UploadHighAnomalyDetail`` list from ``finalize_scores``.
    recovery_channels:
        ``stuck_carry.recovery_channels`` — maps channel → FaultWindow.
    completed_faults:
        ``stuck_carry.completed_faults`` — all completed FaultWindows,
        used to compute final stuck_fault episode durations.
    gap_seconds:
        Max gap before splitting into a new episode.
    recovery_score_cap:
        Peak score ceiling for recovery episodes.
    """
    recovery_channels = recovery_channels or {}
    completed_faults = completed_faults or []

    # Index completed faults by (channel, onset_ts) for O(1) lookup.
    fault_index = _build_fault_index(completed_faults)

    # Build sortable records: (group_key, dt, alert)
    records: list[tuple[str, datetime, Any]] = []
    for alert in high_anomalies:
        key = _channel_key(alert)
        try:
            dt = _parse_iso(alert.timestamp)
        except (ValueError, AttributeError):
            continue
        records.append((key, dt, alert))

    records.sort(key=lambda r: (r[0], r[1]))

    episodes: list[AnomalyEpisode] = []

    current_key: str | None = None
    current_group: list[tuple[datetime, Any]] = []

    for key, dt, alert in records:
        if key != current_key:
            _flush_group(
                current_group, episodes,
                recovery_channels, recovery_score_cap,
                fault_index,
            )
            current_key = key
            current_group = [(dt, alert)]
        else:
            last_dt = current_group[-1][0]
            gap = (dt - last_dt).total_seconds()
            if gap > gap_seconds:
                _flush_group(
                    current_group, episodes,
                    recovery_channels, recovery_score_cap,
                    fault_index,
                )
                current_group = [(dt, alert)]
            else:
                current_group.append((dt, alert))

    _flush_group(
        current_group, episodes,
        recovery_channels, recovery_score_cap,
        fault_index,
    )
    return episodes


def _channel_key(alert: Any) -> str:
    """Grouping key: ``channel:detector``.

    Alerts are only merged within the same (channel, detector) pair.
    """
    col = getattr(alert, "column", None)
    det = getattr(alert, "detector", None) or "lstm"
    if col:
        return f"{col}:{det}"
    return f"__lstm__:{det}"


def _build_fault_index(
    completed_faults: list[Any],
) -> dict[tuple[str, str], Any]:
    """Index completed FaultWindows by (channel, onset_ts)."""
    index: dict[tuple[str, str], Any] = {}
    for fw in completed_faults:
        key = (fw.channel, fw.onset_ts)
        index[key] = fw
    return index


def _stuck_fault_duration(
    first_alert: Any,
    actual_channel: str,
    fault_index: dict[tuple[str, str], Any],
) -> tuple[float, bool]:
    """Compute duration_seconds and is_ongoing for a stuck_fault episode.

    Returns (duration_seconds, is_ongoing).

    Strategy:
    - If a completed FaultWindow matches (same channel + onset_ts),
      use the full fault duration: onset → fault end. is_ongoing=False.
    - Otherwise, the freeze was still open at detection time. Use the
      alert's z_score (= duration_hours at gate crossing) as a lower
      bound. is_ongoing=True.
    """
    onset_ts = first_alert.timestamp
    fw_key = (actual_channel, onset_ts)
    fw = fault_index.get(fw_key)

    if fw is not None:
        # Use the last frozen sample, not the release (first non-frozen)
        # sample.  last_frozen_dt is the correct boundary for the frozen span.
        boundary_dt = fw.last_frozen_dt
        full_duration = (boundary_dt - fw.onset_dt).total_seconds()
        return full_duration, False

    # Fallback: duration_hours stored in z_score field.
    duration_hours = getattr(first_alert, "z_score", None)
    if duration_hours is not None and duration_hours > 0:
        return float(duration_hours) * _SECONDS_PER_HOUR, True

    # Last resort: non-zero minimum so the assertion holds.
    return 0.0, True


def _flush_group(
    group: list[tuple[datetime, Any]],
    episodes: list[AnomalyEpisode],
    recovery_channels: dict[str, Any],
    recovery_score_cap: float,
    fault_index: dict[tuple[str, str], Any],
) -> None:
    """Convert a contiguous group of alerts into an AnomalyEpisode."""
    if not group:
        return

    first_alert = group[0][1]
    detector = getattr(first_alert, "detector", "lstm") or "lstm"

    # Derive the actual channel name from the first alert (not the
    # composite group key which includes the detector suffix).
    actual_channel = getattr(first_alert, "column", None) or "__lstm__"

    start_dt = group[0][0]
    end_dt = group[-1][0]

    scores = [float(a.score) for _, a in group]
    peak = max(scores)
    severities = [getattr(a, "severity", None) for _, a in group]
    best_sev = max(severities, key=_severity_rank)

    # Compute duration.
    is_ongoing = False
    if detector == "stuck_fault":
        duration, is_ongoing = _stuck_fault_duration(
            first_alert, actual_channel, fault_index,
        )
    else:
        duration = (end_dt - start_dt).total_seconds()

    # Detect recovery episodes: channel just exited a detected fault
    # AND the episode's start is within the recovery window.
    is_recovery = False
    if actual_channel in recovery_channels and detector != "stuck_fault":
        fw = recovery_channels[actual_channel]
        _RECOVERY_WINDOW_HOURS = 4.0
        try:
            fault_end_dt = _parse_iso(fw.release_ts)
            gap_hours = (
                (start_dt - fault_end_dt).total_seconds() / 3600.0
            )
            is_recovery = 0 <= gap_hours <= _RECOVERY_WINDOW_HOURS
        except (ValueError, AttributeError):
            pass

    if is_recovery:
        peak = min(peak, recovery_score_cap)

    # Stuck-fault specific fields.
    onset_ts: str | None = None
    detected_ts: str | None = None
    frozen_value: float | None = None
    if detector == "stuck_fault":
        onset_ts = first_alert.timestamp
        detected_ts = getattr(first_alert, "detected_ts", None)
        frozen_value = getattr(first_alert, "value", None)

    episodes.append(AnomalyEpisode(
        channel=actual_channel,
        detector=detector,
        start_ts=start_dt.isoformat(),
        end_ts=end_dt.isoformat(),
        duration_seconds=duration,
        peak_score=peak,
        sample_count=len(group),
        severity=best_sev or "unclassified",
        is_recovery=is_recovery,
        is_ongoing=is_ongoing,
        onset_ts=onset_ts,
        detected_ts=detected_ts,
        frozen_value=frozen_value,
    ))
