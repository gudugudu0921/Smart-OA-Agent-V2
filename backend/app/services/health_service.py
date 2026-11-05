"""Lightweight dependency checks used by health and readiness endpoints."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.mysql import engine
from app.db.redis import RedisUnavailableError, ping_redis


logger = logging.getLogger(__name__)


def check_database() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError as exc:
        logger.warning(
            "Database health check failed",
            extra={"exception_type": type(exc).__name__},
        )
        return False


def check_redis() -> bool:
    try:
        return ping_redis()
    except RedisUnavailableError as exc:
        logger.warning(
            "Redis health check failed",
            extra={"exception_type": type(exc).__name__},
        )
        return False
