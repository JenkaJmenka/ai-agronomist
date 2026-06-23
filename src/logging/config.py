
import logging
import logging.config
import os
import sys
from logging.handlers import TimedRotatingFileHandler

# Define sensitive fields to be redacted
SENSITIVE_FIELDS = {"password", "api_key", "token"}

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

def setup_logging(log_level=logging.INFO):
    """
    Set up structured logging.
    """
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "class": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s"
            },
        },
        "filters": {
            "redacting": {
                "()": RedactingFilter,
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "json",
                "filters": ["redacting"],
            },
            "file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": os.path.join(log_dir, "app.log"),
                "when": "D",
                "interval": 1,
                "backupCount": 7,
                "formatter": "json",
                "filters": ["redacting"],
            },
        },
        "root": {
            "level": log_level,
            "handlers": ["console", "file"],
        },
    })

# Example log entry:
# {
#     "asctime": "2023-10-27T10:00:00.123Z",
#     "name": "root",
#     "levelname": "INFO",
#     "message": "User logged in",
#     "user_id": 123,
#     "password": "***REDACTED***"
# }
