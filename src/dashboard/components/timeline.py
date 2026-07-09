"""Shared date-range controls for monitoring dashboard pages."""

from __future__ import annotations

from datetime import date, time
from typing import Tuple

import pandas as pd
import streamlit as st

from src.dashboard.components.timeline_utils import (
    PAST_PRESET_DEFAULT,
    PAST_PRESET_DELTAS,
    clamp_timeline_bounds,
    combine_date_and_time,
    default_seven_day_window,
    window_from_past_preset,
    _align_timestamp,
    _to_timestamp,
)

PAST_PRESET_LABELS: dict[str, str] = {
    "24h": "Last 24 hours",
    "48h": "Last 48 hours",
    "3d": "Last 3 days",
    "5d": "Last 5 days",
    "7d": "Last 7 days",
    "1m": "Last 1 month",
    "3m": "Last 3 months",
    "6m": "Last 6 months",
}
PAST_PLACEHOLDER = "__please_select__"
PAST_PLACEHOLDER_LABEL = "Please select"

_TIMELINE_SCOPE_KEY = "timeline_scope"
_APPLIED_START_KEY = "time_start"
_APPLIED_END_KEY = "time_end"
_RANGE_MODE_KEY = "date_range_mode"  # "custom" | "past"
_PAST_PRESET_KEY = "date_range_past_preset"
_LAST_APPLY_SOURCE_KEY = "date_range_apply_source"  # "past" | "custom"
_ACTIVE_PREFIX_KEY = "_active_date_range_prefix"

# Legacy alias for monitoring window code paths.
_TIME_START_KEY = _APPLIED_START_KEY
_TIME_END_KEY = _APPLIED_END_KEY


def init_monitoring_session_state(
    tenant: str,
    greenhouse: str,
    data_min: pd.Timestamp,
    data_max: pd.Timestamp,
) -> None:
    """Seed shared date-range keys; reset when tenant/greenhouse or data bounds change."""
    data_min = _to_timestamp(data_min)
    data_max = _to_timestamp(data_max)
    bounds_sig = f"{data_min.isoformat()}|{data_max.isoformat()}"
    scope = f"{tenant}:{greenhouse}|{bounds_sig}"

    if st.session_state.get(_TIMELINE_SCOPE_KEY) != scope:
        st.session_state[_TIMELINE_SCOPE_KEY] = scope
        default_start, default_end = default_seven_day_window(data_min, data_max)
        st.session_state[_APPLIED_START_KEY] = default_start
        st.session_state[_APPLIED_END_KEY] = default_end
        st.session_state[_RANGE_MODE_KEY] = "past"
        st.session_state[_PAST_PRESET_KEY] = PAST_PRESET_DEFAULT
        st.session_state[_LAST_APPLY_SOURCE_KEY] = "past"
        st.session_state["accumulative_climate"] = False
        st.session_state["accumulative_gas"] = False
        return

    if _APPLIED_START_KEY not in st.session_state or _APPLIED_END_KEY not in st.session_state:
        default_start, default_end = default_seven_day_window(data_min, data_max)
        st.session_state.setdefault(_APPLIED_START_KEY, default_start)
        st.session_state.setdefault(_APPLIED_END_KEY, default_end)
    else:
        clamped_start, clamped_end = clamp_timeline_bounds(
            st.session_state[_APPLIED_START_KEY],
            st.session_state[_APPLIED_END_KEY],
            data_min,
            data_max,
        )
        st.session_state[_APPLIED_START_KEY] = clamped_start
        st.session_state[_APPLIED_END_KEY] = clamped_end

    st.session_state.setdefault(_RANGE_MODE_KEY, "past")
    st.session_state.setdefault(_PAST_PRESET_KEY, PAST_PRESET_DEFAULT)
    st.session_state.setdefault(_LAST_APPLY_SOURCE_KEY, "past")
    if "accumulative_climate" not in st.session_state:
        st.session_state["accumulative_climate"] = False
    if "accumulative_gas" not in st.session_state:
        st.session_state["accumulative_gas"] = False


def get_timeline_bounds() -> Tuple[pd.Timestamp, pd.Timestamp]:
    return st.session_state[_APPLIED_START_KEY], st.session_state[_APPLIED_END_KEY]


def _past_select_options() -> list[str]:
    return [PAST_PLACEHOLDER, *PAST_PRESET_DELTAS.keys()]


def _format_past_option(value: str) -> str:
    if value == PAST_PLACEHOLDER:
        return PAST_PLACEHOLDER_LABEL
    return PAST_PRESET_LABELS.get(value, value)


def _clear_from_to_widgets(key_prefix: str) -> None:
    """Reset From/To widget keys so they re-initialize from applied bounds.

    Instead of popping keys (which causes value=None / empty widgets that
    break calendar selection in recent Streamlit versions), we leave the keys
    intact so the picker always shows a selectable date. The widgets will be
    re-synced to the current applied window via _ensure_picker_widgets.
    """
    pass


def _sync_picker_widgets(
    key_prefix: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    """Populate From/To widgets from an applied window."""
    start = _to_timestamp(start)
    end = _to_timestamp(end)
    st.session_state[f"{key_prefix}_from_date"] = start.date()
    st.session_state[f"{key_prefix}_from_time"] = start.time().replace(microsecond=0)
    st.session_state[f"{key_prefix}_to_date"] = end.date()
    st.session_state[f"{key_prefix}_to_time"] = end.time().replace(microsecond=0)


def _ensure_picker_widgets(
    key_prefix: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    """Populate picker widget keys if missing so date/time inputs have values."""
    if f"{key_prefix}_from_date" not in st.session_state:
        _sync_picker_widgets(key_prefix, start, end)


def _process_pending_widget_updates(key_prefix: str, past_select_key: str) -> None:
    """Apply widget-key changes before any widgets are drawn this run."""
    if st.session_state.pop(f"{key_prefix}_pending_clear_pickers", False):
        start = st.session_state.get(_APPLIED_START_KEY)
        end = st.session_state.get(_APPLIED_END_KEY)
        if start is not None and end is not None:
            _sync_picker_widgets(key_prefix, start, end)

    if st.session_state.pop(f"{key_prefix}_pending_past_placeholder", False):
        st.session_state[past_select_key] = PAST_PLACEHOLDER

    if st.session_state.pop(f"{key_prefix}_pending_sync_pickers", False):
        start = st.session_state.get(_APPLIED_START_KEY)
        end = st.session_state.get(_APPLIED_END_KEY)
        if start is not None and end is not None:
            _sync_picker_widgets(key_prefix, start, end)


def _set_custom_mode() -> None:
    st.session_state[_RANGE_MODE_KEY] = "custom"
    st.session_state[_LAST_APPLY_SOURCE_KEY] = "custom"
    prefix = st.session_state.get(_ACTIVE_PREFIX_KEY, "monitor")
    st.session_state[f"{prefix}_pending_past_placeholder"] = True


def _set_past_mode(key_prefix: str) -> None:
    selected = st.session_state.get(f"{key_prefix}_past_select", PAST_PLACEHOLDER)
    if selected == PAST_PLACEHOLDER:
        return
    st.session_state[_RANGE_MODE_KEY] = "past"
    st.session_state[_PAST_PRESET_KEY] = selected
    st.session_state[_LAST_APPLY_SOURCE_KEY] = "past"
    st.session_state[f"{key_prefix}_pending_clear_pickers"] = True


def _draft_from_pickers(
    start_date: date | None,
    start_time: time | None,
    end_date: date | None,
    end_time: time | None,
    data_min: pd.Timestamp,
    data_max: pd.Timestamp,
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    if not all([start_date, start_time, end_date, end_time]):
        return None
    draft_start = _align_timestamp(
        pd.Timestamp(combine_date_and_time(start_date, start_time)),
        data_min,
    )
    draft_end = _align_timestamp(
        pd.Timestamp(combine_date_and_time(end_date, end_time)),
        data_min,
    )
    return clamp_timeline_bounds(draft_start, draft_end, data_min, data_max)


def render_date_range_filters(
    data_min: pd.Timestamp,
    data_max: pd.Timestamp,
    *,
    key_prefix: str = "shared",
    collapsible: bool = False,
    fault_type_options: list[str] | None = None,
    fault_type_labels: dict[str, str] | None = None,
    fault_type_key: str = "fault_type_filter",
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """From/To pickers + Past presets; filters commit on Apply click."""
    if collapsible:
        with st.expander("Filters", expanded=True):
            return _render_date_range_filters_body(
                data_min,
                data_max,
                key_prefix=key_prefix,
                fault_type_options=fault_type_options,
                fault_type_labels=fault_type_labels,
                fault_type_key=fault_type_key,
            )
    return _render_date_range_filters_body(
        data_min,
        data_max,
        key_prefix=key_prefix,
        fault_type_options=fault_type_options,
        fault_type_labels=fault_type_labels,
        fault_type_key=fault_type_key,
    )


def _render_date_range_filters_body(
    data_min: pd.Timestamp,
    data_max: pd.Timestamp,
    *,
    key_prefix: str,
    fault_type_options: list[str] | None,
    fault_type_labels: dict[str, str] | None,
    fault_type_key: str,
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    data_min = _to_timestamp(data_min)
    data_max = _to_timestamp(data_max)
    st.session_state[_ACTIVE_PREFIX_KEY] = key_prefix

    applied_start = _align_timestamp(
        st.session_state.get(_APPLIED_START_KEY, data_min), data_min
    )
    applied_end = _align_timestamp(
        st.session_state.get(_APPLIED_END_KEY, data_max), data_min
    )
    applied_start, applied_end = clamp_timeline_bounds(
        applied_start, applied_end, data_min, data_max
    )

    mode = st.session_state.get(_RANGE_MODE_KEY, "past")
    past_preset = st.session_state.get(_PAST_PRESET_KEY, PAST_PRESET_DEFAULT)
    past_select_key = f"{key_prefix}_past_select"

    if past_select_key not in st.session_state:
        st.session_state[past_select_key] = (
            past_preset if mode == "past" else PAST_PLACEHOLDER
        )

    _process_pending_widget_updates(key_prefix, past_select_key)

    _span_seconds = (data_max - data_min).total_seconds()
    if _span_seconds >= 86400:
        _n = round(_span_seconds / 86400)
        _span_txt = f"{_n} day{'s' if _n != 1 else ''}"
    else:
        _n = max(1, round(_span_seconds / 3600))
        _span_txt = f"{_n} hour{'s' if _n != 1 else ''}"
    st.caption(
        f"Data available **{data_min.strftime('%d %b %Y %H:%M')}** → "
        f"**{data_max.strftime('%d %b %Y %H:%M')}** ({_span_txt} of data). "
        "Use a **custom range** or a **quick range**, then click **Apply**."
    )

    custom_active = mode == "custom"
    past_active = mode == "past"

    from_to_col, preset_col = st.columns([3, 2])

    draft_bounds: tuple[pd.Timestamp, pd.Timestamp] | None = None
    with from_to_col:
        with st.container(border=custom_active):
            st.markdown(
                "**Custom range — selected**"
                if custom_active
                else "**Custom range**"
            )
            _ensure_picker_widgets(key_prefix, applied_start, applied_end)
            picker_col_from, picker_col_to = st.columns(2)
            with picker_col_from:
                start_date = st.date_input(
                    "From date",
                    min_value=data_min.date(),
                    max_value=data_max.date(),
                    key=f"{key_prefix}_from_date",
                    on_change=_set_custom_mode,
                )
                start_time = st.time_input(
                    "From time",
                    key=f"{key_prefix}_from_time",
                    on_change=_set_custom_mode,
                )
            with picker_col_to:
                end_date = st.date_input(
                    "To date",
                    min_value=data_min.date(),
                    max_value=data_max.date(),
                    key=f"{key_prefix}_to_date",
                    on_change=_set_custom_mode,
                )
                end_time = st.time_input(
                    "To time",
                    key=f"{key_prefix}_to_time",
                    on_change=_set_custom_mode,
                )
            if custom_active:
                draft_bounds = _draft_from_pickers(
                    start_date, start_time, end_date, end_time, data_min, data_max
                )

    with preset_col:
        with st.container(border=past_active):
            st.markdown(
                "**Quick range — selected**" if past_active else "**Quick range**"
            )
            st.selectbox(
                "Preset",
                options=_past_select_options(),
                key=past_select_key,
                format_func=_format_past_option,
                on_change=_set_past_mode,
                args=(key_prefix,),
                label_visibility="collapsed",
                help="Rolling window ending at the latest data point.",
            )
        if fault_type_options is not None:
            st.multiselect(
                "Fault type",
                options=fault_type_options,
                default=[],
                format_func=lambda x: (fault_type_labels or {}).get(x, x),
                key=fault_type_key,
                help="Leave empty to show all fault types",
            )

    _current_preset = st.session_state.get(past_select_key, PAST_PLACEHOLDER)
    apply_disabled = past_active and _current_preset == PAST_PLACEHOLDER
    apply_col, _ = st.columns([1, 5])
    if apply_col.button(
        "Apply",
        type="primary",
        key=f"{key_prefix}_apply_range",
        disabled=apply_disabled,
        help=(
            "Pick a quick range or fill in the custom range first."
            if apply_disabled
            else None
        ),
    ):
        use_past = st.session_state.get(_RANGE_MODE_KEY) == "past"
        if use_past:
            preset = st.session_state.get(past_select_key, PAST_PLACEHOLDER)
            if preset == PAST_PLACEHOLDER:
                st.error("Select a preset range, or switch to From/To.")
            else:
                st.session_state[_PAST_PRESET_KEY] = preset
                new_start, new_end = window_from_past_preset(preset, data_min, data_max)
                st.session_state[_APPLIED_START_KEY] = new_start
                st.session_state[_APPLIED_END_KEY] = new_end
                st.session_state[_LAST_APPLY_SOURCE_KEY] = "past"
                st.session_state[f"{key_prefix}_pending_clear_pickers"] = True
                st.rerun()
        else:
            if draft_bounds is None:
                st.error("Fill in all From/To date and time fields.")
            else:
                new_start, new_end = draft_bounds
                st.session_state[_APPLIED_START_KEY] = new_start
                st.session_state[_APPLIED_END_KEY] = new_end
                st.session_state[_LAST_APPLY_SOURCE_KEY] = "custom"
                st.session_state[f"{key_prefix}_pending_past_placeholder"] = True
                st.rerun()

    active_source = st.session_state.get(_LAST_APPLY_SOURCE_KEY, mode)
    if active_source == "past":
        label = PAST_PRESET_LABELS.get(past_preset, past_preset)
        st.caption(
            f"Active filter: **{label}** "
            f"({applied_start.strftime('%d %b %Y %H:%M')} → "
            f"{applied_end.strftime('%d %b %Y %H:%M')})"
        )
    else:
        st.caption(
            f"Active filter: **custom** "
            f"({applied_start.strftime('%d %b %Y %H:%M')} → "
            f"{applied_end.strftime('%d %b %Y %H:%M')})"
        )

    return applied_start, applied_end


def render_timeline_controls(
    data_min: pd.Timestamp,
    data_max: pd.Timestamp,
    *,
    sidebar: bool = False,
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Monitoring pages: full-width date range with Apply."""
    if sidebar:
        with st.sidebar:
            return render_date_range_filters(data_min, data_max, key_prefix="monitor")
    return render_date_range_filters(data_min, data_max, key_prefix="monitor")


# Backward-compatible aliases for the anomaly page.
init_anomaly_session_state = init_monitoring_session_state
render_anomaly_datetime_controls = render_date_range_filters
