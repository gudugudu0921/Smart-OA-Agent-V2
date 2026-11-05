"""Public notification and real-time todo response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    id: int
    type: str
    title: str
    content: str
    related_application_id: str | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UnreadCountResponse(BaseModel):
    unread_count: int


class MarkAllReadResponse(BaseModel):
    updated: int


class TodoItems(BaseModel):
    manager_approvals: int = 0
    hr_approvals: int = 0


class TodoResponse(BaseModel):
    role: str
    total: int
    items: TodoItems
