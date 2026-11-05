"""Approval adapters from the chat graph to leave services."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool
from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.leave import LeaveReviewRequest, ReviewDecision
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
    review_leave_by_hr,
    review_leave_by_hr_idempotent,
    review_leave_by_manager,
    review_leave_by_manager_idempotent,
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


def manager_review_tool(
    db: Session,
    current_user: User,
    application_id: str,
    decision: str,
    comment: str | None = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    try:
        application = review_leave_by_manager(
            db=db,
            application_id=application_id,
            manager=current_user,
            review=LeaveReviewRequest(decision=decision, comment=comment),
            ip_address=ip_address,
        )
    except (LeaveValidationError, LeaveConflictError, LeaveNotFoundError) as exc:
        return _expected_error_result(exc)
    return {
        "success": True,
        "data": serialize_leave_application(application),
    }


def build_manager_review_tool(
    session_factory: SessionFactory,
    current_user: User,
    *,
    operation_id: str,
    confirmed_payload: dict[str, Any],
    ip_address: str | None = None,
) -> BaseTool:
    """Build one confirmed, actor-bound manager review tool for a request."""
    confirmed_application_id = str(confirmed_payload.get("application_id", ""))
    confirmed_decision = confirmed_payload.get("decision")
    confirmed_comment = confirmed_payload.get("comment")

    @tool("manager_review", response_format="content_and_artifact")
    def manager_review(
        application_id: str,
        decision: ReviewDecision,
        comment: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Apply the confirmed decision to one leave assigned to the manager."""
        if current_user.role != "manager":
            result = {
                "success": False,
                "error_type": "permission_denied",
                "message": "当前账号没有执行主管审批的权限。",
                "operation_status": None,
            }
        elif (
            application_id != confirmed_application_id
            or decision != confirmed_decision
            or comment != confirmed_comment
        ):
            result = {
                "success": False,
                "error_type": "confirmation_conflict",
                "message": "审批参数与已确认内容不一致。",
                "operation_status": None,
            }
        else:
            try:
                application, replayed = review_leave_by_manager_idempotent(
                    session_factory=session_factory,
                    manager=current_user,
                    application_id=application_id,
                    review=LeaveReviewRequest(decision=decision, comment=comment),
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
                    "message": "主管审批正在处理中，请勿重复确认。",
                    "operation_status": "PROCESSING",
                }
            except OperationPreviouslyFailedError:
                result = {
                    "success": False,
                    "error_type": "operation_failed",
                    "message": "主管审批此前已失败，不会自动重新执行。",
                    "operation_status": "FAILED",
                }
            except OperationResultUnknownError:
                result = {
                    "success": False,
                    "error_type": "operation_unknown",
                    "message": "主管审批结果暂时无法确认，请查询操作状态。",
                    "operation_status": "UNKNOWN",
                }
            except IdempotencyConflictError:
                result = {
                    "success": False,
                    "error_type": "idempotency_conflict",
                    "message": "业务操作标识已绑定到不同的审批请求。",
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
                    "message": "当前账号没有执行主管审批的权限。",
                    "operation_status": "FAILED",
                }
            except Exception as exc:
                logger.error(
                    "Agent manager review failed",
                    extra={"exception_type": type(exc).__name__},
                )
                result = {
                    "success": False,
                    "error_type": "operation_unknown",
                    "message": "主管审批结果暂时无法确认，请查询操作状态。",
                    "operation_status": "UNKNOWN",
                }
        return json.dumps(result, ensure_ascii=False), result

    return manager_review


def hr_review_tool(
    db: Session,
    current_user: User,
    application_id: str,
    decision: str,
    comment: str | None = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    try:
        application = review_leave_by_hr(
            db=db,
            application_id=application_id,
            hr_user=current_user,
            review=LeaveReviewRequest(decision=decision, comment=comment),
            ip_address=ip_address,
        )
    except (LeaveValidationError, LeaveConflictError, LeaveNotFoundError) as exc:
        return _expected_error_result(exc)
    return {
        "success": True,
        "data": serialize_leave_application(application),
    }


def build_hr_review_tool(
    session_factory: SessionFactory,
    current_user: User,
    *,
    operation_id: str,
    confirmed_payload: dict[str, Any],
    ip_address: str | None = None,
) -> BaseTool:
    """Build one confirmed, actor-bound HR review tool for a request."""
    confirmed_application_id = str(confirmed_payload.get("application_id", ""))
    confirmed_decision = confirmed_payload.get("decision")
    confirmed_comment = confirmed_payload.get("comment")

    @tool("hr_review", response_format="content_and_artifact")
    def hr_review(
        application_id: str,
        decision: ReviewDecision,
        comment: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Apply the confirmed final decision to one HR-reviewable leave."""
        if current_user.role != "hr":
            result = {
                "success": False,
                "error_type": "permission_denied",
                "message": "当前账号没有执行 HR 终审的权限。",
                "operation_status": None,
            }
        elif (
            application_id != confirmed_application_id
            or decision != confirmed_decision
            or comment != confirmed_comment
        ):
            result = {
                "success": False,
                "error_type": "confirmation_conflict",
                "message": "终审参数与已确认内容不一致。",
                "operation_status": None,
            }
        else:
            try:
                application, replayed = review_leave_by_hr_idempotent(
                    session_factory=session_factory,
                    hr_user=current_user,
                    application_id=application_id,
                    review=LeaveReviewRequest(decision=decision, comment=comment),
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
                    "message": "HR 终审正在处理中，请勿重复确认。",
                    "operation_status": "PROCESSING",
                }
            except OperationPreviouslyFailedError:
                result = {
                    "success": False,
                    "error_type": "operation_failed",
                    "message": "HR 终审此前已失败，不会自动重新执行。",
                    "operation_status": "FAILED",
                }
            except OperationResultUnknownError:
                result = {
                    "success": False,
                    "error_type": "operation_unknown",
                    "message": "HR 终审结果暂时无法确认，请查询操作状态。",
                    "operation_status": "UNKNOWN",
                }
            except IdempotencyConflictError:
                result = {
                    "success": False,
                    "error_type": "idempotency_conflict",
                    "message": "业务操作标识已绑定到不同的终审请求。",
                    "operation_status": None,
                }
            except (LeaveValidationError, LeaveConflictError, LeaveNotFoundError) as exc:
                result = {**_expected_error_result(exc), "operation_status": "FAILED"}
            except LeavePermissionError:
                result = {
                    "success": False,
                    "error_type": "permission_denied",
                    "message": "当前账号没有执行 HR 终审的权限。",
                    "operation_status": "FAILED",
                }
            except Exception as exc:
                logger.error(
                    "Agent HR review failed",
                    extra={"exception_type": type(exc).__name__},
                )
                result = {
                    "success": False,
                    "error_type": "operation_unknown",
                    "message": "HR 终审结果暂时无法确认，请查询操作状态。",
                    "operation_status": "UNKNOWN",
                }
        return json.dumps(result, ensure_ascii=False), result

    return hr_review
