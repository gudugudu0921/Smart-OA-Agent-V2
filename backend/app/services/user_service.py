"""User lookup and administrator-managed account operations."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import (
    generate_temporary_password,
    hash_password,
    utc_now_naive,
)
from app.models.user import User
from app.schemas.user import UserCreate, UserRole
from app.services.audit_service import create_audit_log


class UserServiceError(Exception):
    """Base exception for expected account-management failures."""


class UserConflictError(UserServiceError):
    """Raised when a unique user identity already exists."""


class UserValidationError(UserServiceError):
    """Raised when account data violates an organization rule."""


class UserNotFoundError(UserServiceError):
    """Raised when an account does not exist."""


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.query(User).filter(User.id == user_id).first()


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username).first()


def get_users(db: Session, skip: int = 0, limit: int = 100) -> list[User]:
    return db.query(User).order_by(User.id).offset(skip).limit(limit).all()


def _validate_unique_identity(db: Session, payload: UserCreate) -> None:
    if get_user_by_username(db, payload.username) is not None:
        raise UserConflictError("USERNAME_ALREADY_EXISTS")
    if (
        db.query(User)
        .filter(User.employee_no == payload.employee_no)
        .first()
        is not None
    ):
        raise UserConflictError("EMPLOYEE_NO_ALREADY_EXISTS")
    if payload.email is not None:
        if (
            db.query(User)
            .filter(User.email == str(payload.email))
            .first()
            is not None
        ):
            raise UserConflictError("EMAIL_ALREADY_EXISTS")


def _validated_manager_id(db: Session, payload: UserCreate) -> int | None:
    if payload.role != UserRole.EMPLOYEE:
        if payload.manager_id is not None:
            raise UserValidationError("MANAGER_ID_NOT_ALLOWED_FOR_ROLE")
        return None

    if payload.manager_id is None:
        raise UserValidationError("EMPLOYEE_MANAGER_REQUIRED")
    manager = get_user_by_id(db, payload.manager_id)
    if manager is None:
        raise UserValidationError("MANAGER_NOT_FOUND")
    if manager.role != UserRole.MANAGER.value:
        raise UserValidationError("MANAGER_ROLE_REQUIRED")
    if not manager.is_active:
        raise UserValidationError("MANAGER_INACTIVE")
    return manager.id


def create_user(
    db: Session,
    payload: UserCreate,
    *,
    actor_user_id: int | None = None,
    ip_address: str | None = None,
) -> tuple[User, str]:
    """Create one account and return its plaintext password exactly once."""
    _validate_unique_identity(db, payload)
    manager_id = _validated_manager_id(db, payload)
    temporary_password = generate_temporary_password()
    user = User(
        username=payload.username,
        employee_no=payload.employee_no,
        password_hash=hash_password(temporary_password),
        name=payload.name,
        email=str(payload.email).lower() if payload.email is not None else None,
        role=payload.role.value,
        department=payload.department,
        position=payload.position,
        manager_id=manager_id,
        is_active=True,
        must_change_password=True,
        last_login_at=None,
        password_changed_at=None,
    )
    db.add(user)
    try:
        db.flush()
        if actor_user_id is not None:
            create_audit_log(
                db=db,
                user_id=actor_user_id,
                action="USER_CREATED",
                resource_type="user",
                resource_id=str(user.id),
                detail={
                    "target_user_id": user.id,
                    "username": user.username,
                    "role": user.role,
                },
                ip_address=ip_address,
            )
        db.commit()
        db.refresh(user)
    except IntegrityError as exc:
        db.rollback()
        raise UserConflictError("USER_IDENTITY_ALREADY_EXISTS") from exc
    except Exception:
        db.rollback()
        raise
    return user, temporary_password


def set_user_active_status(
    db: Session,
    *,
    user_id: int,
    is_active: bool,
    actor_user_id: int,
    ip_address: str | None = None,
) -> User:
    user = get_user_by_id(db, user_id)
    if user is None:
        raise UserNotFoundError("USER_NOT_FOUND")
    if user.id == actor_user_id and not is_active:
        raise UserValidationError("CANNOT_DISABLE_SELF")
    if user.is_active == is_active:
        return user

    user.is_active = is_active
    create_audit_log(
        db=db,
        user_id=actor_user_id,
        action="USER_ACTIVATED" if is_active else "USER_DEACTIVATED",
        resource_type="user",
        resource_id=str(user.id),
        detail={"target_user_id": user.id, "is_active": is_active},
        ip_address=ip_address,
    )
    try:
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise
    return user


def reset_user_password(
    db: Session,
    *,
    user_id: int,
    actor_user_id: int,
    ip_address: str | None = None,
) -> tuple[User, str]:
    user = get_user_by_id(db, user_id)
    if user is None:
        raise UserNotFoundError("USER_NOT_FOUND")
    if user.id == actor_user_id:
        raise UserValidationError("CANNOT_RESET_SELF_PASSWORD")

    temporary_password = generate_temporary_password()
    user.password_hash = hash_password(temporary_password)
    user.must_change_password = True
    user.password_changed_at = utc_now_naive()
    create_audit_log(
        db=db,
        user_id=actor_user_id,
        action="PASSWORD_RESET",
        resource_type="user",
        resource_id=str(user.id),
        detail={
            "target_user_id": user.id,
            "admin_user_id": actor_user_id,
        },
        ip_address=ip_address,
    )
    try:
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise
    return user, temporary_password
