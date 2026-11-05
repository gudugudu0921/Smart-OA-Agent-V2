"""Authentication and authorization dependencies for FastAPI routes."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import (
    AccessTokenError,
    AccessTokenExpiredError,
    decode_access_token,
    normalize_utc,
    token_issued_at,
)
from app.db.mysql import get_db
from app.models.user import User
from app.services.user_service import get_user_by_id


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def _unauthorized_exception(
    code: str = "INVALID_TOKEN",
    message: str = "登录凭证无效，请重新登录。",
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
        if user_id <= 0:
            raise ValueError("User id must be positive")
    except AccessTokenExpiredError:
        raise _unauthorized_exception(
            "TOKEN_EXPIRED",
            "登录状态已过期，请重新登录。",
        )
    except (AccessTokenError, KeyError, TypeError, ValueError):
        raise _unauthorized_exception()

    user = get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        raise _unauthorized_exception()
    if user.password_changed_at is not None:
        try:
            issued_at = token_issued_at(payload)
        except AccessTokenError:
            raise _unauthorized_exception()
        if issued_at <= normalize_utc(user.password_changed_at):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="TOKEN_REVOKED_BY_PASSWORD_CHANGE",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return user


def get_current_business_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Reject normal business access until a temporary password is changed."""
    if current_user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PASSWORD_CHANGE_REQUIRED",
                "message": "首次登录需要修改临时密码后才能继续使用系统。",
            },
        )
    return current_user


def require_roles(*allowed_roles: str) -> Callable[..., User]:
    """Create a deterministic dependency that permits only selected roles."""
    allowed = frozenset(allowed_roles)
    if not allowed:
        raise ValueError("At least one allowed role is required")

    def role_checker(
        current_user: User = Depends(get_current_business_user),
    ) -> User:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "PERMISSION_DENIED",
                    "message": "没有权限执行该操作。",
                },
            )
        return current_user

    return role_checker
