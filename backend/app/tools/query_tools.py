"""Read-only adapters from the chat graph to leave services."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool
from sqlalchemy.orm import Session

from app.models.leave_application import LeaveApplication
from app.models.user import User
from app.services.leave_service import (
    get_hr_pending_applications,
    get_manager_pending_applications,
    get_my_leave_applications,
)


logger = logging.getLogger(__name__)


def serialize_leave_application(
    application: LeaveApplication,
    include_employee: bool = False,
) -> dict[str, Any]:
    """Convert an ORM object into the limited structure exposed to the agent."""
    data = {
        "application_id": application.id,
        "leave_type": application.leave_type,
        "start_date": application.start_date.isoformat(),
        "end_date": application.end_date.isoformat(),
        "leave_days": float(application.leave_days),
        "status": application.status,
        "manager_status": application.manager_status,
        "hr_status": application.hr_status,
    }
    if include_employee:
        data["employee"] = {
            "id": application.employee.id,
            "employee_no": application.employee.employee_no,
            "name": application.employee.name,
        }
    return data


def query_my_leave_tool(db: Session, current_user: User) -> dict[str, Any]:
    applications = get_my_leave_applications(
        db=db,
        employee_id=current_user.id,
        limit=20,
    )
    return {
        "success": True,
        "data": [serialize_leave_application(item) for item in applications],
    }


def build_query_my_leave_tool(db: Session, current_user: User) -> BaseTool:
    """Build the request-scoped LLM tool without exposing trusted context."""

    @tool("query_my_leave", response_format="content_and_artifact")
    def query_my_leave() -> tuple[str, dict[str, Any]]:
        """Query leave applications belonging to the authenticated employee."""
        if current_user.role != "employee":
            result = {
                "success": False,
                "error_type": "permission_denied",
                "message": "当前账号没有执行该操作的权限。",
            }
        else:
            try:
                result = query_my_leave_tool(db, current_user)
            except Exception as exc:
                logger.error(
                    "Agent leave query failed",
                    extra={"exception_type": type(exc).__name__},
                )
                result = {
                    "success": False,
                    "error_type": "query_failed",
                    "message": "查询服务暂时不可用，请稍后重试。",
                }
        return json.dumps(result, ensure_ascii=False), result

    return query_my_leave


def build_query_manager_pending_tool(db: Session, current_user: User) -> BaseTool:
    """Build the request-scoped manager query tool with trusted context bound."""

    @tool("query_manager_pending", response_format="content_and_artifact")
    def query_manager_pending() -> tuple[str, dict[str, Any]]:
        """Query leave applications awaiting the authenticated manager's review."""
        if current_user.role != "manager":
            result = {
                "success": False,
                "error_type": "permission_denied",
                "message": "当前账号没有执行该操作的权限。",
            }
        else:
            try:
                result = query_manager_pending_tool(db, current_user)
            except Exception as exc:
                logger.error(
                    "Agent manager pending query failed",
                    extra={"exception_type": type(exc).__name__},
                )
                result = {
                    "success": False,
                    "error_type": "query_failed",
                    "message": "查询服务暂时不可用，请稍后重试。",
                }
        return json.dumps(result, ensure_ascii=False), result

    return query_manager_pending


def build_query_hr_pending_tool(db: Session, current_user: User) -> BaseTool:
    """Build the request-scoped HR query tool with trusted context bound."""

    @tool("query_hr_pending", response_format="content_and_artifact")
    def query_hr_pending() -> tuple[str, dict[str, Any]]:
        """Query leave applications awaiting HR review for an authenticated HR user."""
        if current_user.role != "hr":
            result = {
                "success": False,
                "error_type": "permission_denied",
                "message": "当前账号没有执行该操作的权限。",
            }
        else:
            try:
                result = query_hr_pending_tool(db, current_user)
            except Exception as exc:
                logger.error(
                    "Agent HR pending query failed",
                    extra={"exception_type": type(exc).__name__},
                )
                result = {
                    "success": False,
                    "error_type": "query_failed",
                    "message": "查询服务暂时不可用，请稍后重试。",
                }
        return json.dumps(result, ensure_ascii=False), result

    return query_hr_pending


def query_manager_pending_tool(db: Session, current_user: User) -> dict[str, Any]:
    applications = get_manager_pending_applications(
        db=db,
        manager_id=current_user.id,
        limit=20,
    )
    return {
        "success": True,
        "data": [
            serialize_leave_application(item, include_employee=True)
            for item in applications
        ],
    }


def query_hr_pending_tool(db: Session, current_user: User) -> dict[str, Any]:
    applications = get_hr_pending_applications(db=db, limit=20)
    return {
        "success": True,
        "data": [
            serialize_leave_application(item, include_employee=True)
            for item in applications
        ],
    }
