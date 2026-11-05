"""Audit-log creation helpers."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.request_context import get_request_id
from app.models.audit_log import AuditLog
from app.models.user import User


logger = logging.getLogger(__name__)


def create_audit_log(
    db: Session,
    user_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    detail: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Add an audit record to the current transaction without committing it."""
    audit_log = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
        ip_address=ip_address,
        request_id=get_request_id(),
    )
    db.add(audit_log)
    return audit_log


def record_login_audit(
    db: Session,
    *,
    action: str,
    user_id: int | None,
    username: str,
    reason_code: str,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """Persist login audit best-effort without changing the auth outcome."""
    try:
        create_audit_log(
            db=db,
            user_id=user_id,
            action=action,
            resource_type="authentication",
            resource_id=None,
            detail={
                "username": username[:64],
                "result": action,
                "reason_code": reason_code,
                "user_agent": user_agent,
            },
            ip_address=ip_address,
        )
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(
            "Login audit persistence failed",
            extra={
                "audit_action": action,
                "exception_type": type(exc).__name__,
            },
        )


def get_audit_logs(
    db: Session,
    *,
    action: str | None = None,
    resource_type: str | None = None,
    user_id: int | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return read-only audit rows with non-sensitive user display fields."""
    query = db.query(AuditLog, User.employee_no, User.name).outerjoin(
        User, AuditLog.user_id == User.id
    )
    if action is not None:
        query = query.filter(AuditLog.action == action)
    if resource_type is not None:
        query = query.filter(AuditLog.resource_type == resource_type)
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    rows = (
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "employee_no": employee_no,
            "name": name,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "detail": log.detail,
            "ip_address": log.ip_address,
            "request_id": log.request_id,
            "created_at": log.created_at,
        }
        for log, employee_no, name in rows
    ]
