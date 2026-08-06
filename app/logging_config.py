import json
import logging
import logging.config


_SAFE_EXTRA_FIELDS = (
    "scope",
    "operation",
    "relationship_name",
    "duration_ms",
    "status",
    "error_code",
    "returncode",
    "stderr_preview",
)


class _SafeJsonFormatter(logging.Formatter):
    """Produce valid JSON log lines regardless of message content.

    The previous approach used a %-format string to build JSON, which produced
    unparsable output whenever the message contained double-quotes or newlines
    (e.g. PowerShell stderr with embedded quotes). Using json.dumps guarantees
    correct escaping for any message content.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        for field in _SAFE_EXTRA_FIELDS:
            if hasattr(record, field):
                value = getattr(record, field)
                if value is not None:
                    payload[field] = value

        return json.dumps(payload, ensure_ascii=False, default=str)


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": _SafeJsonFormatter,
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},  # overridden by configure_logging(level)
}


def configure_logging(level: str = "INFO") -> None:
    logging.config.dictConfig({
        **LOGGING_CONFIG,
        "root": {"handlers": ["console"], "level": level.upper()},
    })
