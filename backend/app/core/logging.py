"""Small JSON console logging configuration for the application."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.request_context import get_request_id


_STRUCTURED_FIELDS = (
    "method",
    "path",
    "status_code",
    "duration_ms",
    "exception_type",
    "audit_action",
    "error_code",
    "safe_traceback",
)


class JsonLogFormatter(logging.Formatter):
    """Emit predictable JSON without serializing arbitrary LogRecord data."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "request_id": getattr(record, "request_id", None) or get_request_id(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Configure one stdout/stderr-friendly handler without a file dependency."""
    app_logger = logging.getLogger("app")
    if any(
        getattr(handler, "_smart_oa_handler", False)
        for handler in app_logger.handlers
    ):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    handler._smart_oa_handler = True  # type: ignore[attr-defined]
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
