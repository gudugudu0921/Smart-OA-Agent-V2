"""Central exception types, safe API responses, and handler registration."""

from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import (
    DisconnectionError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import REQUEST_ID_HEADER, get_request_id


logger = logging.getLogger("app.exceptions")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")

ERROR_MESSAGES = {
    "AUTHENTICATION_REQUIRED": "请先登录后再继续操作。",
    "AUTHENTICATION_FAILED": "用户名或密码不正确。",
    "INVALID_TOKEN": "登录凭证无效，请重新登录。",
    "TOKEN_EXPIRED": "登录状态已过期，请重新登录。",
    "TOKEN_REVOKED_BY_PASSWORD_CHANGE": "登录状态已失效，请重新登录。",
    "PERMISSION_DENIED": "没有权限执行该操作。",
    "NOT_FOUND": "请求的资源不存在。",
    "PASSWORD_CHANGE_REQUIRED": "首次登录需要修改临时密码后才能继续使用系统。",
    "USERNAME_ALREADY_EXISTS": "用户名已存在。",
    "EMPLOYEE_NO_ALREADY_EXISTS": "员工编号已存在。",
    "EMAIL_ALREADY_EXISTS": "邮箱已存在。",
    "USER_IDENTITY_ALREADY_EXISTS": "用户账号、员工编号或邮箱已存在。",
    "USER_NOT_FOUND": "用户不存在。",
    "MANAGER_NOT_FOUND": "直属主管不存在。",
    "MANAGER_ROLE_REQUIRED": "所选直属主管不具备主管角色。",
    "MANAGER_INACTIVE": "所选直属主管已停用。",
    "EMPLOYEE_MANAGER_REQUIRED": "员工账号必须指定直属主管。",
    "MANAGER_ID_NOT_ALLOWED_FOR_ROLE": "该角色不能设置直属主管。",
    "CANNOT_DISABLE_SELF": "不能停用当前登录账号。",
    "CANNOT_RESET_SELF_PASSWORD": "不能通过管理员功能重置自己的密码。",
    "CURRENT_PASSWORD_INCORRECT": "当前密码不正确。",
    "NEW_PASSWORD_MUST_DIFFER": "新密码不能与当前密码相同。",
    "PASSWORD_TOO_SHORT": "密码长度不能少于 8 个字符。",
    "PASSWORD_TOO_LONG": "密码长度超出限制。",
    "PASSWORD_TOO_WEAK": "密码强度不足。",
}


class AppException(Exception):
    """An expected API failure with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.headers = headers


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None) or get_request_id()


def _response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    detail: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    response_headers = dict(headers or {})
    if request_id:
        response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=status_code,
        headers=response_headers,
        content={
            "success": False,
            "code": code,
            "message": message,
            "detail": message if detail is None else detail,
            "request_id": request_id,
        },
    )


def _safe_traceback(exc: Exception) -> str:
    """Return stack frames without the exception message or local variables."""
    return " <- ".join(
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    )


def _log_failure(
    request: Request,
    level: int,
    code: str,
    exc: Exception,
) -> None:
    logger.log(
        level,
        "API request failed",
        extra={
            "error_code": code,
            "exception_type": type(exc).__name__,
            "safe_traceback": _safe_traceback(exc),
            "request_id": _request_id(request),
        },
    )


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return _response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        headers=exc.headers,
    )


def _default_http_code(status_code: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "AUTHENTICATION_REQUIRED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        409: "CONFLICT",
        503: "SERVICE_UNAVAILABLE",
    }.get(status_code, "HTTP_ERROR")


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    raw_detail = exc.detail
    code = _default_http_code(exc.status_code)
    message: str
    if isinstance(raw_detail, dict):
        candidate = raw_detail.get("code")
        if isinstance(candidate, str) and _ERROR_CODE.fullmatch(candidate):
            code = candidate
        candidate_message = raw_detail.get("message")
        message = (
            candidate_message
            if isinstance(candidate_message, str)
            else ERROR_MESSAGES.get(code, "请求处理失败。")
        )
    elif isinstance(raw_detail, str):
        if _ERROR_CODE.fullmatch(raw_detail):
            code = raw_detail
        elif exc.status_code == status.HTTP_401_UNAUTHORIZED:
            code = (
                "AUTHENTICATION_REQUIRED"
                if raw_detail == "Not authenticated"
                else "AUTHENTICATION_FAILED"
            )
        message = ERROR_MESSAGES.get(code, raw_detail)
    else:
        message = "请求处理失败。"
    return _response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        detail=raw_detail,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    safe_errors = [
        {
            "location": [str(part) for part in error.get("loc", ())],
            "message": error.get("msg", "Invalid value"),
            "type": error.get("type", "validation_error"),
        }
        for error in exc.errors()
    ]
    message = "请求参数不正确。"
    return _response(
        request,
        status_code=422,
        code="VALIDATION_ERROR",
        message=message,
        detail={"message": message, "errors": safe_errors},
    )


async def integrity_exception_handler(
    request: Request,
    exc: IntegrityError,
) -> JSONResponse:
    _log_failure(request, logging.WARNING, "DATA_CONFLICT", exc)
    return _response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        code="DATA_CONFLICT",
        message="数据状态发生冲突，请检查后重试。",
    )


async def database_exception_handler(
    request: Request,
    exc: SQLAlchemyError,
) -> JSONResponse:
    if isinstance(
        exc,
        (OperationalError, InterfaceError, DisconnectionError, SQLAlchemyTimeoutError),
    ):
        code = "DATABASE_UNAVAILABLE"
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        message = "数据库服务暂时不可用，请稍后重试。"
    else:
        code = "DATABASE_ERROR"
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        message = "系统暂时出现异常，请稍后重试。"
    _log_failure(request, logging.ERROR, code, exc)
    return _response(
        request,
        status_code=status_code,
        code=code,
        message=message,
    )


async def unknown_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    _log_failure(request, logging.ERROR, "INTERNAL_SERVER_ERROR", exc)
    message = "系统暂时出现异常，请稍后重试。"
    return _response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="INTERNAL_SERVER_ERROR",
        message=message,
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers after route modules are loaded to avoid import cycles."""
    from app.core.llm import LLMConfigurationError, LLMUnavailableError
    from app.db.redis import RedisUnavailableError
    from app.services.auth_service import PasswordChangeError
    from app.services.leave_service import (
        LeaveConflictError,
        LeaveNotFoundError,
        LeaveValidationError,
    )
    from app.services.notification_service import NotificationNotFoundError
    from app.services.session_service import SessionNotFoundError
    from app.services.user_service import (
        UserConflictError,
        UserNotFoundError,
        UserValidationError,
    )

    async def business_handler(request: Request, exc: Exception) -> JSONResponse:
        raw_code = str(exc)
        if isinstance(exc, UserConflictError):
            code, http_status = raw_code, status.HTTP_409_CONFLICT
        elif isinstance(exc, UserNotFoundError):
            code, http_status = raw_code, status.HTTP_404_NOT_FOUND
        elif isinstance(exc, (UserValidationError, PasswordChangeError)):
            code, http_status = raw_code, status.HTTP_400_BAD_REQUEST
        elif isinstance(exc, LeaveValidationError):
            code, http_status = "LEAVE_VALIDATION_ERROR", status.HTTP_400_BAD_REQUEST
        elif isinstance(exc, LeaveConflictError):
            code, http_status = "LEAVE_CONFLICT", status.HTTP_409_CONFLICT
        elif isinstance(exc, LeaveNotFoundError):
            code, http_status = "LEAVE_NOT_FOUND", status.HTTP_404_NOT_FOUND
        elif isinstance(exc, NotificationNotFoundError):
            code, http_status = "NOTIFICATION_NOT_FOUND", status.HTTP_404_NOT_FOUND
        else:
            code, http_status = "SESSION_NOT_FOUND", status.HTTP_404_NOT_FOUND
        return _response(
            request,
            status_code=http_status,
            code=code,
            message=ERROR_MESSAGES.get(code, raw_code),
        )

    async def redis_handler(
        request: Request, exc: RedisUnavailableError
    ) -> JSONResponse:
        _log_failure(request, logging.WARNING, "REDIS_UNAVAILABLE", exc)
        return _response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REDIS_UNAVAILABLE",
            message="智能助手会话服务暂时不可用，请稍后重试。",
        )

    async def llm_config_handler(
        request: Request, exc: LLMConfigurationError
    ) -> JSONResponse:
        _log_failure(request, logging.WARNING, "LLM_NOT_CONFIGURED", exc)
        return _response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="LLM_NOT_CONFIGURED",
            message="智能助手暂时不可用，请稍后重试。",
        )

    async def llm_unavailable_handler(
        request: Request, exc: LLMUnavailableError
    ) -> JSONResponse:
        _log_failure(request, logging.WARNING, "LLM_UNAVAILABLE", exc)
        return _response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="LLM_UNAVAILABLE",
            message="智能助手暂时不可用，请稍后重试。",
        )

    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    for exception_type in (
        UserConflictError,
        UserNotFoundError,
        UserValidationError,
        PasswordChangeError,
        LeaveValidationError,
        LeaveConflictError,
        LeaveNotFoundError,
        NotificationNotFoundError,
        SessionNotFoundError,
    ):
        app.add_exception_handler(exception_type, business_handler)
    app.add_exception_handler(RedisUnavailableError, redis_handler)
    app.add_exception_handler(LLMConfigurationError, llm_config_handler)
    app.add_exception_handler(LLMUnavailableError, llm_unavailable_handler)
    app.add_exception_handler(IntegrityError, integrity_exception_handler)
    app.add_exception_handler(SQLAlchemyError, database_exception_handler)
    app.add_exception_handler(Exception, unknown_exception_handler)
