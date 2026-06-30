"""Assign stable, unique colors per sensor within a dashboard chart."""

from __future__ import annotations

from typing import Dict, List, Sequence

import streamlit as st

# Plotly qualitative palettes (Bold + Safe + Pastel + Vivid + Dark24 subset)
_RAW_PALETTE: List[str] = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A", "#19D3F3",
    "#FF6692", "#B6E880", "#FF97FF", "#FECB52", "#1F77B4", "#FF7F0E",
    "#2CA02C", "#D62728", "#9467BD", "#8C564B", "#E377C2", "#7F7F7F",
    "#BCBD22", "#17BECF", "#393B79", "#637939", "#8C6D31", "#843C39",
    "#7B4173", "#5254A3", "#6B6ECF", "#9C9EDE", "#B5CF6B",
    "#CEDB9C", "#E7BA52", "#E7CB94", "#BD9E39", "#E7969C",
    "#D6616B", "#AD494A", "#A55194", "#CE6DBD", "#DE9ED6", "#3182BD",
]

_BASE_PALETTE: List[str] = list(dict.fromkeys(_RAW_PALETTE))

_GOLDEN_ANGLE = 137.508
_SESSION_MANAGERS_KEY = "_chart_color_managers"


def color_at_index(index: int) -> str:
    """Return a distinct chart color for the given zero-based index."""
    if index < len(_BASE_PALETTE):
        return _BASE_PALETTE[index]
    hue = (index * _GOLDEN_ANGLE) % 360.0
    return f"hsl({hue:.1f}, 68%, 55%)"


class ColorAssignmentManager:
    """Maps sensors to unique colors within a tab context."""

    def __init__(self, palette: List[str] | None = None) -> None:
        self.color_palette = palette or _BASE_PALETTE
        self.sensor_colors: Dict[str, str] = {}
        self._used_colors: set[str] = set()
        self._next_index = 0

    def assign_colors(self, sensor_names: Sequence[str], tab_context: str) -> Dict[str, str]:
        """Ensure every sensor in the chart has a unique color."""
        for name in sensor_names:
            self.get_color(name, tab_context)
        return {
            name: self.sensor_colors[f"{tab_context}_{name}"]
            for name in sensor_names
        }

    def get_color(self, sensor_name: str, tab_context: str) -> str:
        key = f"{tab_context}_{sensor_name}"
        if key in self.sensor_colors:
            return self.sensor_colors[key]

        while True:
            candidate = color_at_index(self._next_index)
            self._next_index += 1
            if candidate not in self._used_colors:
                self.sensor_colors[key] = candidate
                self._used_colors.add(candidate)
                return candidate


def get_chart_color_manager(tab_context: str) -> ColorAssignmentManager:
    """Return a session-persisted color manager so lines keep color when sensors are added."""
    if _SESSION_MANAGERS_KEY not in st.session_state:
        st.session_state[_SESSION_MANAGERS_KEY] = {}
    managers: dict[str, ColorAssignmentManager] = st.session_state[_SESSION_MANAGERS_KEY]
    if tab_context not in managers:
        managers[tab_context] = ColorAssignmentManager()
    return managers[tab_context]
