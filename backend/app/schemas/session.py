"""Schemas for Redis-backed chat sessions."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.chat import ChatConfirmation, OperationStatus


class SessionCreateResponse(BaseModel):
    thread_id: UUID
    title: str
    created_at: datetime


class SessionSummary(BaseModel):
    thread_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    last_message_preview: str


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class SessionMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class SessionMessagesResponse(BaseModel):
    thread_id: UUID
    messages: list[SessionMessage]


SessionTaskStatus = Literal[
    "idle",
    "collecting",
    "awaiting_confirmation",
    "executing",
    "succeeded",
    "failed",
    "unknown",
]


class SessionTaskResponse(BaseModel):
    """Public, read-only projection of one checkpointed chat task."""

    thread_id: UUID
    operation_id: UUID | None = None
    task_status: SessionTaskStatus
    task_revision: Annotated[int, Field(strict=True, ge=0)]
    requires_confirmation: bool
    confirmation: ChatConfirmation | None = None
    operation_status: OperationStatus | None = None
    code: str
