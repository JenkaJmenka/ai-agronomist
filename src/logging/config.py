
import logging
import logging.config
import os
import sys

# Define sensitive fields to be redacted
SENSITIVE_FIELDS = {"password", "api_key", "token"}

# Module-level guard so repeated calls are a no-op.
_logging_configured = False


class RedactingFilter(logging.Filter):
    """
    A logging filter to redact sensitive information.
    """
    def filter(self, record):
        if isinstance(record.args, dict):
            for key, value in record.args.items():
                if key in SENSITIVE_FIELDS:
                    record.args[key] = "***REDACTED***"
        return True


def _is_running_under_pytest() -> bool:
    """Detect whether the process was launched by pytest."""
    return "PYTEST_CURRENT_TEST" in os.environ or "PYTEST_VERSION" in os.environ


def setup_logging(log_level=logging.INFO):
    """
    Set up structured logging.

    Idempotent: subsequent calls after the first are no-ops.
    Pytest-safe: does not create a ``logs/`` directory or file handler when
    running under pytest, and preserves any pre-existing root handlers
    (e.g. pytest's ``LogCaptureHandler``) that ``dictConfig`` would remove.
    """
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True

    root = logging.getLogger()
    pre_existing_handlers = list(root.handlers)

    is_test = _is_running_under_pytest()

    # Build the dict-config payload.
    config: dict = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "class": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s",
            },
        },
        "filters": {
            "redacting": {
                "()": RedactingFilter,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "json",
                "filters": ["redacting"],
            },
        },
        "root": {
            "level": log_level,
            "handlers": ["console"],
        },
    }

    if not is_test:
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        config["handlers"]["file"] = {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "filename": os.path.join(log_dir, "app.log"),
            "when": "D",
            "interval": 1,
            "backupCount": 7,
            "formatter": "json",
            "filters": ["redacting"],
        }
        config["root"]["handlers"].append("file")

    logging.config.dictConfig(config)

    # Re-attach any handler that dictConfig removed but did not create
    # (e.g. pytest's LogCaptureHandler).
    post_handlers = set(id(h) for h in root.handlers)
    for handler in pre_existing_handlers:
        if id(handler) not in post_handlers:
            root.addHandler(handler)

# Example log entry:
# {
#     "asctime": "2023-10-27T10:00:00.123Z",
#     "name": "root",
#     "levelname": "INFO",
#     "message": "User logged in",
#     "user_id": 123,
#     "password": "***REDACTED***"
# }
