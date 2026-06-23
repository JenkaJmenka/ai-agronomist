"""Four-panel line chart for a 20×4 sensor sequence."""

from __future__ import annotations

from typing import Any, List, Optional

import plotly.graph_objects as go
import streamlit as st

SENSORS = [
    ("temperature", "Temperature (°C)"),
    ("humidity", "Humidity (%)"),
    ("co2", "CO₂ (ppm)"),
    ("light", "Light (lux)"),
]


def render(sequence: Optional[List[List[float]]], title: str = "Sensor sequence") -> None:
    """Render 4-panel subplot for one sequence."""
    if not sequence:
        st.info("No sequence data available.")
        return

    import numpy as np

    arr = np.asarray(sequence, dtype=float)
    if arr.ndim != 2 or arr.shape[1] < 4:
        st.warning("Sequence must be shape (seq_len, 4).")
        return

    fig = go.Figure()
    steps = list(range(arr.shape[0]))
    for idx, (key, label) in enumerate(SENSORS):
        fig.add_trace(
            go.Scatter(
                x=steps,
                y=arr[:, idx],
                mode="lines",
                name=label,
                xaxis="x" if idx < 2 else "x2",
                yaxis="y" if idx < 2 else "y2",
            )
        )

    fig.update_layout(
        title=title,
        height=400,
        showlegend=True,
    )
    st.plotly_chart(fig, use_container_width=True)
