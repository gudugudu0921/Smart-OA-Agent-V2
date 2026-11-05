"""Persistent idempotency records for authenticated business operations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CHAR,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import relationship

from app.db.mysql import Base


BUSINESS_OPERATION_TYPES = (
    "SUBMIT_LEAVE",
    "CANCEL_LEAVE",
    "MANAGER_REVIEW",
    "HR_REVIEW",
)
BUSINESS_OPERATION_STATUSES = (
    "PROCESSING",
    "SUCCEEDED",
    "FAILED",
    "UNKNOWN",
)


def utc_now_naive() -> datetime:
    """Return UTC without tzinfo for MySQL ``DATETIME`` columns."""
    return datetime.now(UTC).replace(tzinfo=None)


UTC_DATETIME = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
FINGERPRINT_CHAR = CHAR(64).with_variant(
    mysql.CHAR(64, charset="ascii", collation="ascii_bin"),
    "mysql",
)


class BusinessOperation(Base):
    """One actor-bound idempotency record; no business execution lives here."""

    __tablename__ = "business_operations"
    __table_args__ = (
        CheckConstraint(
            "operation_type IN ('SUBMIT_LEAVE', 'CANCEL_LEAVE', "
            "'MANAGER_REVIEW', 'HR_REVIEW')",
            name="chk_business_operations_type",
        ),
        CheckConstraint(
            "status IN ('PROCESSING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name="chk_business_operations_status",
        ),
        CheckConstraint(
            "fingerprint_version > 0",
            name="chk_business_operations_fingerprint_version",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="chk_business_operations_attempt_count",
        ),
        Index(
            "idx_business_operations_actor_created",
            "actor_user_id",
            "created_at",
        ),
        Index(
            "idx_business_operations_status_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "idx_business_operations_resource",
            "resource_type",
            "resource_id",
        ),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    operation_id = Column(String(36), primary_key=True)
    actor_user_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_business_operations_actor_user_id_users"),
        nullable=False,
    )
    operation_type = Column(String(32), nullable=False)
    fingerprint_version = Column(
        SmallInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    request_fingerprint = Column(FINGERPRINT_CHAR, nullable=False)
    status = Column(
        String(24),
        nullable=False,
        default="PROCESSING",
        server_default=text("'PROCESSING'"),
    )
    resource_type = Column(String(32), nullable=True)
    resource_id = Column(String(64), nullable=True)
    result_payload = Column(JSON, nullable=True)
    error_code = Column(String(64), nullable=True)
    lease_owner = Column(String(36), nullable=True)
    lease_expires_at = Column(UTC_DATETIME, nullable=True)
    attempt_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    thread_id = Column(String(36), nullable=True)
    first_request_id = Column(String(64), nullable=True)
    created_at = Column(
        UTC_DATETIME,
        nullable=False,
        default=utc_now_naive,
    )
    updated_at = Column(
        UTC_DATETIME,
        nullable=False,
        default=utc_now_naive,
        onupdate=utc_now_naive,
    )
    completed_at = Column(UTC_DATETIME, nullable=True)

    actor = relationship("User")
