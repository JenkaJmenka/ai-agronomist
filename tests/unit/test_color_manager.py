"""Tests for unique chart color assignment."""

from src.dashboard.components.color_manager import ColorAssignmentManager, color_at_index


def test_color_at_index_returns_unique_for_first_twenty():
    colors = [color_at_index(i) for i in range(20)]
    assert len(colors) == len(set(colors))


def test_manager_assigns_unique_colors_per_chart():
    manager = ColorAssignmentManager()
    sensors = [f"sensor_{i}" for i in range(15)]
    assigned = manager.assign_colors(sensors, "climate")
    assert len(assigned) == 15
    assert len(set(assigned.values())) == 15


def test_manager_keeps_stable_color_for_same_sensor():
    manager = ColorAssignmentManager()
    first = manager.get_color("outdoor_temp", "monitoring")
    second = manager.get_color("outdoor_temp", "monitoring")
    assert first == second


def test_manager_preserves_colors_when_adding_sensor():
    manager = ColorAssignmentManager()
    initial = manager.assign_colors(["temp", "humidity"], "monitoring")
    expanded = manager.assign_colors(["humidity", "temp", "co2"], "monitoring")
    assert expanded["temp"] == initial["temp"]
    assert expanded["humidity"] == initial["humidity"]
    assert expanded["co2"] not in {initial["temp"], initial["humidity"]}
