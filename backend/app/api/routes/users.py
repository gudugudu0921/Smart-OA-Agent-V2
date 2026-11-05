"""Authenticated user routes."""

from typing import List

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, require_roles
from app.core.request_context import get_client_ip
from app.db.mysql import get_db
from app.models.user import User
from app.schemas.user import (
    UserCreate,
    UserCreateResponse,
    UserPasswordResetResponse,
    UserResponse,
    UserStatusUpdate,
)
from app.services.user_service import (
    create_user,
    get_users,
    reset_user_password,
    set_user_active_status,
)


router = APIRouter()


@router.get("/me", response_model=UserResponse)
def read_current_user(
    current_user: User = Depends(get_current_user),
) -> User:
    return current_user


@router.get("/", response_model=List[UserResponse])
def read_users(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_roles("admin", "hr")),
) -> List[User]:
    return get_users(db, skip=skip, limit=limit)


@router.post(
    "/",
    response_model=UserCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_managed_user(
    payload: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_roles("admin")),
) -> UserCreateResponse:
    user, temporary_password = create_user(
        db,
        payload,
        actor_user_id=admin.id,
        ip_address=get_client_ip(request),
    )
    return UserCreateResponse(
        user=UserResponse.model_validate(user),
        temporary_password=temporary_password,
    )


@router.patch("/{user_id}/status", response_model=UserResponse)
def update_user_status(
    user_id: int,
    payload: UserStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_roles("admin")),
) -> User:
    return set_user_active_status(
        db,
        user_id=user_id,
        is_active=payload.is_active,
        actor_user_id=admin.id,
        ip_address=get_client_ip(request),
    )


@router.post(
    "/{user_id}/reset-password",
    response_model=UserPasswordResetResponse,
)
def reset_managed_user_password(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_roles("admin")),
) -> UserPasswordResetResponse:
    user, temporary_password = reset_user_password(
        db,
        user_id=user_id,
        actor_user_id=admin.id,
        ip_address=get_client_ip(request),
    )
    return UserPasswordResetResponse(
        user_id=user.id,
        username=user.username,
        temporary_password=temporary_password,
    )
