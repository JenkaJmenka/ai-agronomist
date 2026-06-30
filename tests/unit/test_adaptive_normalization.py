"""Tests for adaptive sensor normalization."""

import numpy as np

from src.config import TEMP_RANGE, HUMIDITY_RANGE, CO2_RANGE, LIGHT_RANGE
from src.models.data_utils import compute_adaptive_sensor_ranges, normalize_data, SENSOR_RANGES


def test_adaptive_ranges_cover_winter_greenhouse_data():
  # Winter greenhouse temps below synthetic training floor (20 °C).
  data = np.array(
      [
          [13.0, 52.0, 400.0, 0.0],
          [18.5, 78.0, 400.0, 200.0],
          [25.0, 90.0, 400.0, 800.0],
      ],
      dtype=np.float32,
  )
  ranges = compute_adaptive_sensor_ranges(data, SENSOR_RANGES)
  normed = normalize_data(data, ranges)
  assert normed.min() >= -0.01
  assert normed.max() <= 1.01
  assert ranges[0][0] < TEMP_RANGE[0]
  assert ranges[1][0] <= 52.0


def test_flat_column_falls_back_to_default_range():
  data = np.array([[20.0, 65.0, 400.0, 400.0]], dtype=np.float32)
  ranges = compute_adaptive_sensor_ranges(data, SENSOR_RANGES)
  assert ranges[2] == (CO2_RANGE[0], CO2_RANGE[1])
  assert ranges[3] == (LIGHT_RANGE[0], LIGHT_RANGE[1])
