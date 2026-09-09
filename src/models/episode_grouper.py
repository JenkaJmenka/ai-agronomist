"""Episode grouping for anomaly alerts (Tasks 3 + 5).

Groups contiguous per-channel alerts into episodes so one continuous
event is one alert with a duration instead of N rows.

**Design decision (documented in docs/architecture/episode_grouping.md):**
grouping lives at the detection layer — inside ``finalize_scores`` —
because:
  1. Per-sample scores survive in ``anomaly_scores`` for the timeline chart.
  2. The episode identity is computed once and never re-derived.
  3. No migration needed — episodes are a view over ``high_anomalies``.
  4. Bounded memory: state is O(active_channels), not O(file_length).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config import EPISODE_GAP_SECONDS, RECOVERY_SCORE_CAP


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
            "onset_ts": self.onset_ts,
            "detected_ts": self.detected_ts,
            "frozen_value": self.frozen_value,
        }


def _severity_rank(sev: str | None) -> int:
    """Numeric rank for severity comparison (higher = more severe)."""
    ranks = {"normal": 0, "warning": 1, "critical": 2, "urgent": 3}
    return ranks.get(sev or "normal", 0)


def group_alerts_into_episodes(
    high_anomalies: list[Any],
    *,
    recovery_channels: dict[str, Any] | None = None,
    gap_seconds: int = EPISODE_GAP_SECONDS,
    recovery_score_cap: float = RECOVERY_SCORE_CAP,
) -> list[AnomalyEpisode]:
    """Group high_anomalies into contiguous per-channel episodes.

    Two alerts belong to the same episode when they share a channel
    (or are both channel-less LSTM hits) and their timestamps are
    within ``gap_seconds`` of each other.

    Stuck-fault alerts are one-sample episodes by construction (one
    alert per emitted_onsets key).  Recovery episodes — statistical
    alerts on channels that just exited a detected fault — are tagged
    ``is_recovery=True`` and their peak score is capped.

    Parameters
    ----------
    high_anomalies:
        ``UploadHighAnomalyDetail`` list from ``finalize_scores``.
    recovery_channels:
        ``stuck_carry.recovery_channels`` — maps channel → FaultWindow.
    gap_seconds:
        Max gap before splitting into a new episode.
    recovery_score_cap:
        Peak score ceiling for recovery episodes.
    """
    recovery_channels = recovery_channels or {}

    # Build sortable records: (channel_key, dt, alert)
    records: list[tuple[str, datetime, Any]] = []
    for alert in high_anomalies:
        ch = _channel_key(alert)
        try:
            dt = _parse_iso(alert.timestamp)
        except (ValueError, AttributeError):
            continue
        records.append((ch, dt, alert))

    records.sort(key=lambda r: (r[0], r[1]))

    episodes: list[AnomalyEpisode] = []
    _flush_group([], None, None, episodes, recovery_channels, recovery_score_cap)  # no-op init

    current_channel: str | None = None
    current_group: list[tuple[datetime, Any]] = []

    for ch, dt, alert in records:
        if ch != current_channel:
            _flush_group(
                current_group, current_channel, None,
                episodes, recovery_channels, recovery_score_cap,
            )
            current_channel = ch
            current_group = [(dt, alert)]
        else:
            last_dt = current_group[-1][0]
            gap = (dt - last_dt).total_seconds()
            if gap > gap_seconds:
                _flush_group(
                    current_group, current_channel, None,
                    episodes, recovery_channels, recovery_score_cap,
                )
                current_group = [(dt, alert)]
            else:
                current_group.append((dt, alert))

    _flush_group(
        current_group, current_channel, None,
        episodes, recovery_channels, recovery_score_cap,
    )
    return episodes


def _channel_key(alert: Any) -> str:
    """Extract a grouping key from an alert."""
    col = getattr(alert, "column", None)
    det = getattr(alert, "detector", None) or "lstm"
    if col:
        return f"{col}"
    return f"__lstm__{det}"


def _flush_group(
    group: list[tuple[datetime, Any]],
    channel: str | None,
    _unused: Any,
    episodes: list[AnomalyEpisode],
    recovery_channels: dict[str, Any],
    recovery_score_cap: float,
) -> None:
    """Convert a contiguous group of alerts into an AnomalyEpisode."""
    if not group or channel is None:
        return

    start_dt = group[0][0]
    end_dt = group[-1][0]
    duration = (end_dt - start_dt).total_seconds()

    scores = [float(a.score) for _, a in group]
    peak = max(scores)
    severities = [getattr(a, "severity", None) for _, a in group]
    best_sev = max(severities, key=_severity_rank)

    detector = getattr(group[0][1], "detector", "lstm") or "lstm"

    # Detect recovery episodes: channel just exited a detected fault AND
    # the episode's start is within the recovery window (2× rolling window
    # worth of samples ≈ 4 h after fault end at 5-min cadence).  Episodes
    # far from a fault end are genuine independent excursions, not recovery.
    is_recovery = False
    if channel in recovery_channels and detector != "stuck_fault":
        fw = recovery_channels[channel]
        # Recovery window: from fault end to fault end + 4 hours.
        # Any episode that starts within this window is a post-fault
        # transition, not a genuine excursion.
        _RECOVERY_WINDOW_HOURS = 4.0
        try:
            fault_end_dt = _parse_iso(fw.end_ts)
            episode_start_dt = start_dt
            gap_hours = (
                (episode_start_dt - fault_end_dt).total_seconds() / 3600.0
            )
            is_recovery = 0 <= gap_hours <= _RECOVERY_WINDOW_HOURS
        except (ValueError, AttributeError):
            pass

    if is_recovery:
        peak = min(peak, recovery_score_cap)

    # Stuck-fault specific fields
    first_alert = group[0][1]
    onset_ts: str | None = None
    detected_ts: str | None = None
    frozen_value: float | None = None
    if detector == "stuck_fault":
        onset_ts = first_alert.timestamp
        detected_ts = getattr(first_alert, "detected_ts", None)
        frozen_value = getattr(first_alert, "value", None)

    episodes.append(AnomalyEpisode(
        channel=channel,
        detector=detector,
        start_ts=start_dt.isoformat(),
        end_ts=end_dt.isoformat(),
        duration_seconds=duration,
        peak_score=peak,
        sample_count=len(group),
        severity=best_sev or "normal",
        is_recovery=is_recovery,
        onset_ts=onset_ts,
        detected_ts=detected_ts,
        frozen_value=frozen_value,
    ))
