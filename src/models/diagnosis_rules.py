"""Data tables for root cause diagnosis: rules, indicator evidence, and severity classifiers."""

from typing import Any, Dict

# Pattern types that indicate high severity when matched.
_HIGH_SEVERITY_PATTERNS = frozenset({"cascade", "dependency"})

# Pattern types that indicate medium severity when matched.
_MEDIUM_SEVERITY_PATTERNS = frozenset({"drift", "saturation"})

# Human-readable evidence for each indicator token used in DIAGNOSIS_RULES.
_INDICATOR_EVIDENCE: Dict[str, str] = {
    "temp_decline": "Temperature showed clear declining trend",
    "humidity_spike": "Humidity showed significant spike",
    "humidity_decline": "Humidity showed clear declining trend",
    "high_temp": "Temperature elevated above normal operating range",
    "co2_decline": "CO2 level showed clear declining trend",
    "plant_stress": "Sensor pattern consistent with plant stress",
    "zero_variance": "Sensor shows zero or very low variance",
    "constant_reading": "Sensor reading is constant",
    "rapid_increase": "Rapid value change detected",
    "high_peak": "Value reached an unusually high peak",
    "spike_then_flat": "Sudden spike followed by flat reading",
    "boundary_value": "Value at sensor boundary (min/max)",
    "no_control": "Control system appears unresponsive",
    "coordinated_failure": "Multiple sensors deviated simultaneously",
    "multiple_sensors": "Multiple sensors affected",
    "temp_up": "Temperature trending upward",
    "humidity_up": "Humidity trending upward",
    "co2_down": "CO2 level trending downward",
}

DIAGNOSIS_RULES: Dict[tuple, Dict[str, Any]] = {
    # --- Dependency patterns ---
    ("dependency", "light_temp"): {
        "cause": "ventilation_failure",
        "confidence": 0.85,
        "description": (
            "Lights on but temperature dropping indicates cooling "
            "system active without ventilation"
        ),
        "indicators": ["temp_decline", "humidity_spike"],
    },
    ("dependency", "light_humidity"): {
        "cause": "heating_efficiency_loss",
        "confidence": 0.80,
        "description": (
            "Heating active but humidity dropping indicates "
            "excessive dehumidification"
        ),
        "indicators": ["humidity_decline", "high_temp"],
    },
    ("dependency", "light_co2"): {
        "cause": "co2_supply_failure",
        "confidence": 0.80,
        "description": (
            "Lights on but CO2 dropping indicates supply "
            "system failure"
        ),
        "indicators": ["co2_decline", "plant_stress"],
    },
    # --- Drift patterns ---
    ("drift", "temperature"): {
        "cause": "sensor_failure",
        "confidence": 0.75,
        "description": "Temperature sensor stuck at constant value",
        "indicators": ["zero_variance", "constant_reading"],
    },
    ("drift", "humidity"): {
        "cause": "humidity_sensor_failure",
        "confidence": 0.75,
        "description": "Humidity sensor stuck",
        "indicators": ["zero_variance"],
    },
    ("drift", "co2"): {
        "cause": "co2_sensor_failure",
        "confidence": 0.75,
        "description": "CO2 sensor stuck",
        "indicators": ["zero_variance"],
    },
    ("drift", "light"): {
        "cause": "sensor_malfunction",
        "confidence": 0.72,
        "description": (
            "Light sensor stuck at constant value indicates hardware malfunction"
        ),
        "indicators": ["zero_variance", "constant_reading"],
    },
    # --- Spike patterns ---
    ("spike", "temperature"): {
        "cause": "heating_overshoot",
        "confidence": 0.70,
        "description": (
            "Temperature spike indicates heating system overshoot"
        ),
        "indicators": ["rapid_increase", "high_peak"],
    },
    ("spike", "humidity"): {
        "cause": "humidity_controller_failure",
        "confidence": 0.65,
        "description": (
            "Humidity spike indicates misting system malfunction"
        ),
        "indicators": ["rapid_increase"],
    },
    ("spike", "co2"): {
        "cause": "co2_supply_blockage",
        "confidence": 0.70,
        "description": (
            "CO2 levels spike then stuck indicates supply blockage"
        ),
        "indicators": ["spike_then_flat"],
    },
    # --- Saturation patterns ---
    ("saturation", "humidity_max"): {
        "cause": "humidity_limit_exceeded",
        "confidence": 0.80,
        "description": (
            "Humidity at maximum indicates system cannot dehumidify"
        ),
        "indicators": ["boundary_value", "no_control"],
    },
    # --- Cascade patterns ---
    ("cascade", "multi_sensor"): {
        "cause": "power_supply_issue",
        "confidence": 0.75,
        "description": (
            "Multiple sensors failing together indicates power "
            "or communication issue"
        ),
        "indicators": ["coordinated_failure", "multiple_sensors"],
    },
    ("cascade", "cooling_related"): {
        "cause": "compressor_failure",
        "confidence": 0.80,
        "description": "Cooling system components failing together",
        "indicators": ["temp_up", "humidity_up", "co2_down"],
    },
}
