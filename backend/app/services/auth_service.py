"""Authentication service operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session

from app.core.security import (
    PasswordPolicyError,
    hash_password,
    utc_now_naive,
    validate_password_strength,
    verify_password,
)
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.user_service import get_user_by_username


class PasswordChangeError(ValueError):
    """Raised for an expected self-service password change failure."""


class AuthenticationReason(str, Enum):
    SUCCESS = "SUCCESS"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    USER_INACTIVE = "USER_INACTIVE"
    INVALID_PASSWORD = "INVALID_PASSWORD"


@dataclass(frozen=True)
class AuthenticationResult:
    user: User | None
    reason: AuthenticationReason


def authenticate_user_with_reason(
    db: Session,
    username: str,
    password: str,
) -> AuthenticationResult:
    user = get_user_by_username(db, username)
    if user is None:
        return AuthenticationResult(None, AuthenticationReason.USER_NOT_FOUND)
    if not user.is_active:
        return AuthenticationResult(user, AuthenticationReason.USER_INACTIVE)
    if not verify_password(password, user.password_hash):
        return AuthenticationResult(user, AuthenticationReason.INVALID_PASSWORD)
    user.last_login_at = utc_now_naive()
    try:
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise
    return AuthenticationResult(user, AuthenticationReason.SUCCESS)


def authenticate_user(
    db: Session,
    username: str,
    password: str,
) -> User | None:
    """Backward-compatible authentication API used by existing callers/tests."""
    result = authenticate_user_with_reason(db, username, password)
    return result.user if result.reason is AuthenticationReason.SUCCESS else None


def change_password(
    db: Session,
    *,
    user: User,
    old_password: str,
    new_password: str,
    ip_address: str | None = None,
) -> User:
    if not verify_password(old_password, user.password_hash):
        raise PasswordChangeError("CURRENT_PASSWORD_INCORRECT")
    if verify_password(new_password, user.password_hash):
        raise PasswordChangeError("NEW_PASSWORD_MUST_DIFFER")
    try:
        validate_password_strength(new_password)
    except PasswordPolicyError as exc:
        raise PasswordChangeError(str(exc)) from exc

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = utc_now_naive()
    create_audit_log(
        db=db,
        user_id=user.id,
        action="PASSWORD_CHANGED",
        resource_type="user",
        resource_id=str(user.id),
        detail={"target_user_id": user.id},
        ip_address=ip_address,
    )
    try:
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise
    return user
