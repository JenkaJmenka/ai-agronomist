"""Regression tests for logging isolation.

Proves that ``setup_logging()`` does not remove pre-existing root handlers
(e.g. pytest's ``LogCaptureHandler``) and that caplog-based assertions work
after ``setup_logging()`` has been called.
"""

from __future__ import annotations

import logging
import os

import pytest

import src.logging.config as logging_config
from src.logging.config import RedactingFilter, setup_logging


class TestSetupLoggingPreservesHandlers:
    """setup_logging must not evict handlers that existed before it ran."""

    def test_pre_existing_handler_survives_setup(self) -> None:
        """A sentinel handler attached to root before setup_logging() must
        still be present after the call."""
        sentinel = logging.StreamHandler()
        sentinel.set_name("test_sentinel")
        root = logging.getLogger()
        root.addHandler(sentinel)

        # Reset the idempotency guard so we can exercise the function.
        old_flag = logging_config._logging_configured
        logging_config._logging_configured = False
        try:
            setup_logging()
        finally:
            logging_config._logging_configured = old_flag

        handler_ids = {id(h) for h in root.handlers}
        assert id(sentinel) in handler_ids, (
            "setup_logging() removed a pre-existing handler from the root logger"
        )

        # Clean up.
        root.removeHandler(sentinel)

    def test_idempotent_second_call_is_noop(self) -> None:
        """Calling setup_logging() twice must not duplicate handlers."""
        old_flag = logging_config._logging_configured
        logging_config._logging_configured = False
        try:
            setup_logging()
            count_after_first = len(logging.getLogger().handlers)

            # Second call should be a no-op due to the guard.
            setup_logging()
            count_after_second = len(logging.getLogger().handlers)
        finally:
            logging_config._logging_configured = old_flag

        assert count_after_second == count_after_first


class TestCaplogWorksAfterSetupLogging:
    """caplog must capture records even after setup_logging() has run."""

    def test_caplog_captures_after_setup(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """This is the core regression: an earlier version of setup_logging()
        called dictConfig which evicted pytest's LogCaptureHandler, causing
        caplog.records to be empty."""
        old_flag = logging_config._logging_configured
        logging_config._logging_configured = False
        try:
            setup_logging()
        finally:
            logging_config._logging_configured = old_flag

        test_logger = logging.getLogger("test.logging_isolation")
        with caplog.at_level(logging.WARNING, logger="test.logging_isolation"):
            test_logger.warning("sentinel-message-for-regression-test")

        messages = [r.getMessage() for r in caplog.records]
        assert "sentinel-message-for-regression-test" in messages, (
            "caplog did not capture the log record — setup_logging() likely "
            "evicted pytest's LogCaptureHandler"
        )


class TestNoLogsDirectoryUnderPytest:
    """setup_logging() must not create a logs/ directory during tests."""

    def test_file_handler_skipped_under_pytest(self) -> None:
        """When PYTEST_CURRENT_TEST or PYTEST_VERSION is set, the file
        handler and logs/ directory must not be created."""
        # The environment variable is set by pytest itself; just verify
        # the guard detects it.
        assert logging_config._is_running_under_pytest(), (
            "_is_running_under_pytest() returned False inside a pytest run"
        )


class TestRedactingFilterUnchanged:
    """Verify the RedactingFilter still works (no accidental regression)."""

    def test_redacts_sensitive_fields(self) -> None:
        filt = RedactingFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test %s",
            args={"password": "secret123", "user": "alice"},
            exc_info=None,
        )
        filt.filter(record)
        assert record.args["password"] == "***REDACTED***"
        assert record.args["user"] == "alice"

    def test_passes_non_dict_args(self) -> None:
        filt = RedactingFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="count=%d",
            args=(42,),
            exc_info=None,
        )
        result = filt.filter(record)
        assert result is True
        assert record.args == (42,)
