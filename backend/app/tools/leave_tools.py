"""Mutation adapters from the chat graph to leave services."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from langchain_core.tools import BaseTool, tool
from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.leave import LeaveCreate, LeaveType
from app.services.idempotency_service import (
    IdempotencyConflictError,
    OperationInProgressError,
    OperationPreviouslyFailedError,
    OperationResultUnknownError,
)
from app.services.leave_service import (
    LeaveConflictError,
    LeaveNotFoundError,
    LeavePermissionError,
    LeaveValidationError,
    SessionFactory,
    cancel_leave_application,
    cancel_leave_application_idempotent,
    create_leave_application,
    create_leave_application_idempotent,
)
from app.tools.query_tools import serialize_leave_application


logger = logging.getLogger(__name__)


def _expected_error_result(error: Exception) -> dict[str, Any]:
    if isinstance(error, LeaveValidationError):
        error_type = "validation"
    elif isinstance(error, LeaveConflictError):
        error_type = "conflict"
    else:
        error_type = "not_found"
    return {
        "success": False,
        "error_type": error_type,
        "message": str(error),
    }


def submit_leave_tool(
    db: Session,
    current_user: User,
    leave_data: dict[str, Any],
    ip_address: str | None = None,
) -> dict[str, Any]:
    try:
        application = create_leave_application(
            db=db,
            employee=current_user,
            payload=LeaveCreate.model_validate(leave_data),
            ip_address=ip_address,
        )
    except (LeaveValidationError, LeaveConflictError, LeaveNotFoundError) as exc:
        return _expected_error_result(exc)
    return {
        "success": True,
        "data": serialize_leave_application(application),
    }


def build_submit_leave_tool(
    session_factory: SessionFactory,
    current_user: User,
    *,
    operation_id: str,
    confirmed_payload: dict[str, Any],
    ip_address: str | None = None,
) -> BaseTool:
    """Build one confirmed, actor-bound leave submission tool for a request."""
    confirmed = LeaveCreate.model_validate(confirmed_payload).model_dump(mode="json")

    @tool("submit_leave", response_format="content_and_artifact")
    def submit_leave(
        leave_type: LeaveType,
        start_date: date,
        end_date: date,
        reason: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Submit the exact leave request already confirmed by the employee."""
        payload = LeaveCreate(
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
        )
        if current_user.role != "employee":
            result = {
                "success": False,
                "error_type": "permission_denied",
                "message": "当前账号没有提交请假申请的权限。",
                "operation_status": None,
            }
        elif payload.model_dump(mode="json") != confirmed:
            result = {
                "success": False,
                "error_type": "confirmation_conflict",
                "message": "提交参数与已确认内容不一致。",
                "operation_status": None,
            }
        else:
            try:
                application = create_leave_application_idempotent(
                    session_factory=session_factory,
                    employee=current_user,
                    payload=payload,
                    operation_id=operation_id,
                    ip_address=ip_address,
                )
                result = {
                    "success": True,
                    "data": serialize_leave_application(application),
                    "operation_status": "SUCCEEDED",
                    "replayed": False,
                }
            except OperationInProgressError:
                result = {
                    "success": False,
                    "error_type": "operation_in_progress",
                    "message": "请假提交正在处理中，请勿重复确认。",
                    "operation_status": "PROCESSING",
                }
            except OperationPreviouslyFailedError:
                result = {
                    "success": False,
                    "error_type": "operation_failed",
                    "message": "请假提交此前已失败，不会自动重新执行。",
                    "operation_status": "FAILED",
                }
            except OperationResultUnknownError:
                result = {
                    "success": False,
                    "error_type": "operation_unknown",
                    "message": "请假提交结果暂时无法确认，请查询操作状态。",
                    "operation_status": "UNKNOWN",
                }
            except IdempotencyConflictError:
                result = {
                    "success": False,
                    "error_type": "idempotency_conflict",
                    "message": "业务操作标识已绑定到不同的请假请求。",
                    "operation_status": None,
                }
            except (LeaveValidationError, LeaveConflictError, LeaveNotFoundError) as exc:
                result = {**_expected_error_result(exc), "operation_status": "FAILED"}
            except LeavePermissionError:
                result = {
                    "success": False,
                    "error_type": "permission_denied",
                    "message": "当前账号没有提交请假申请的权限。",
                    "operation_status": "FAILED",
                }
            except Exception as exc:
                logger.error(
                    "Agent leave submission failed",
                    extra={"exception_type": type(exc).__name__},
                )
                result = {
                    "success": False,
                    "error_type": "operation_unknown",
                    "message": "请假提交结果暂时无法确认，请查询操作状态。",
                    "operation_status": "UNKNOWN",
                }
        return json.dumps(result, ensure_ascii=False), result

    return submit_leave


def cancel_leave_tool(
    db: Session,
    current_user: User,
    application_id: str,
    ip_address: str | None = None,
) -> dict[str, Any]:
    try:
        application = cancel_leave_application(
            db=db,
            application_id=application_id,
            employee=current_user,
            ip_address=ip_address,
        )
    except (LeaveValidationError, LeaveConflictError, LeaveNotFoundError) as exc:
        return _expected_error_result(exc)
    return {
        "success": True,
        "data": serialize_leave_application(application),
    }


def build_cancel_leave_tool(
    session_factory: SessionFactory,
    current_user: User,
    *,
    operation_id: str,
    confirmed_payload: dict[str, Any],
    ip_address: str | None = None,
) -> BaseTool:
    """Build a confirmed, actor-bound cancellation tool for one request."""
    confirmed_application_id = str(confirmed_payload.get("application_id", ""))

    @tool("cancel_leave", response_format="content_and_artifact")
    def cancel_leave(application_id: str) -> tuple[str, dict[str, Any]]:
        """Cancel the confirmed leave application owned by the authenticated employee."""
        if current_user.role != "employee":
            result = {
                "success": False,
                "error_type": "permission_denied",
                "message": "当前账号没有执行该操作的权限。",
                "operation_status": None,
            }
        elif application_id != confirmed_application_id:
            result = {
                "success": False,
                "error_type": "confirmation_conflict",
                "message": "撤销目标与已确认申请不一致。",
                "operation_status": None,
            }
        else:
            try:
                application, replayed = cancel_leave_application_idempotent(
                    session_factory=session_factory,
                    employee=current_user,
                    application_id=application_id,
                    operation_id=operation_id,
                    confirmed_payload=confirmed_payload,
                    ip_address=ip_address,
                )
                result = {
                    "success": True,
                    "data": serialize_leave_application(application),
                    "operation_status": "SUCCEEDED",
                    "replayed": replayed,
                }
            except OperationInProgressError:
                result = {
                    "success": False,
                    "error_type": "operation_in_progress",
                    "message": "撤销操作正在处理中，请勿重复确认。",
                    "operation_status": "PROCESSING",
                }
            except OperationPreviouslyFailedError:
                result = {
                    "success": False,
                    "error_type": "operation_failed",
                    "message": "撤销操作此前已失败，不会自动重新执行。",
                    "operation_status": "FAILED",
                }
            except OperationResultUnknownError:
                result = {
                    "success": False,
                    "error_type": "operation_unknown",
                    "message": "撤销结果暂时无法确认，请查询操作状态。",
                    "operation_status": "UNKNOWN",
                }
            except IdempotencyConflictError:
                result = {
                    "success": False,
                    "error_type": "idempotency_conflict",
                    "message": "业务操作标识已绑定到不同的撤销请求。",
                    "operation_status": None,
                }
            except (LeaveValidationError, LeaveConflictError, LeaveNotFoundError) as exc:
                result = {
                    **_expected_error_result(exc),
                    "operation_status": "FAILED",
                }
            except LeavePermissionError:
                result = {
                    "success": False,
                    "error_type": "permission_denied",
                    "message": "当前账号没有执行该操作的权限。",
                    "operation_status": "FAILED",
                }
            except Exception as exc:
                logger.error(
                    "Agent leave cancellation failed",
                    extra={"exception_type": type(exc).__name__},
                )
                result = {
                    "success": False,
                    "error_type": "operation_unknown",
                    "message": "撤销结果暂时无法确认，请查询操作状态。",
                    "operation_status": "UNKNOWN",
                }
        return json.dumps(result, ensure_ascii=False), result

    return cancel_leave
