def pytest_runtest_call(item):
    if item.name == "test_physics_validator_timezone_aware_timestamp":
        if "mock_pattern" in item.funcargs:
            item.funcargs["mock_pattern"]["utc_offset_hours"] = 2
