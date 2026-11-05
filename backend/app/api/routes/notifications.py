"""Authenticated in-app notification and MySQL todo routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_business_user
from app.db.mysql import get_db
from app.models.user import User
from app.schemas.notification import (
    MarkAllReadResponse,
    NotificationResponse,
    TodoResponse,
    UnreadCountResponse,
)
from app.services.notification_service import (
    get_notifications,
    get_todos,
    get_unread_count,
    mark_all_notifications_read,
    mark_notification_read,
)


router = APIRouter()


@router.get("", response_model=list[NotificationResponse])
def read_notifications(
    unread_only: bool = False,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_business_user),
) -> list[NotificationResponse]:
    return get_notifications(
        db,
        current_user.id,
        unread_only=unread_only,
        skip=skip,
        limit=limit,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
def read_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_business_user),
) -> UnreadCountResponse:
    return UnreadCountResponse(unread_count=get_unread_count(db, current_user.id))


@router.get("/todos", response_model=TodoResponse)
def read_todos(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_business_user),
) -> TodoResponse:
    return TodoResponse.model_validate(get_todos(db, current_user))


@router.post("/read-all", response_model=MarkAllReadResponse)
def read_all_notifications(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_business_user),
) -> MarkAllReadResponse:
    return MarkAllReadResponse(updated=mark_all_notifications_read(db, current_user.id))


@router.post("/{notification_id}/read", response_model=NotificationResponse)
def read_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_business_user),
) -> NotificationResponse:
    return mark_notification_read(db, current_user.id, notification_id)
