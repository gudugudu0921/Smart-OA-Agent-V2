"""Chat API request and response schemas."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.leave import LeaveType, ReviewDecision


TaskAction = Literal["message", "confirm", "cancel"]
TaskStatus = Literal[
    "collecting",
    "awaiting_confirmation",
    "executing",
    "succeeded",
    "failed",
    "unknown",
    "cancelled",
]
OperationStatus = Literal["PROCESSING", "SUCCEEDED", "FAILED", "UNKNOWN"]
TaskRevision = Annotated[int, Field(strict=True, ge=0)]


class SubmitLeaveConfirmation(BaseModel):
    """Validated submit-leave snapshot safe for confirmation UI rendering."""

    model_config = ConfigDict(extra="forbid")

    leave_type: LeaveType
    start_date: date
    end_date: date
    reason: str | None = None
    task_revision: TaskRevision


class CancelLeaveConfirmation(BaseModel):
    """Validated cancellation target safe for confirmation UI rendering."""

    model_config = ConfigDict(extra="forbid")

    operation_type: Literal["CANCEL_LEAVE"]
    application_id: UUID
    leave_type: LeaveType
    start_date: date
    end_date: date
    status: Literal["pending"]
    task_revision: TaskRevision


class ManagerReviewConfirmation(BaseModel):
    """Validated manager decision safe for confirmation UI rendering."""

    model_config = ConfigDict(extra="forbid")

    operation_type: Literal["MANAGER_REVIEW"]
    application_id: UUID
    employee_name: str
    employee_no: str
    leave_type: LeaveType
    start_date: date
    end_date: date
    status: Literal["pending"]
    decision: ReviewDecision
    comment: str | None = None
    task_revision: TaskRevision


class HrReviewConfirmation(BaseModel):
    """Validated HR final decision safe for confirmation UI rendering."""

    model_config = ConfigDict(extra="forbid")

    operation_type: Literal["HR_REVIEW"]
    application_id: UUID
    employee_name: str
    employee_no: str
    leave_type: LeaveType
    start_date: date
    end_date: date
    status: Literal["pending"]
    decision: ReviewDecision
    comment: str | None = None
    task_revision: TaskRevision


ChatConfirmation = (
    SubmitLeaveConfirmation
    | CancelLeaveConfirmation
    | ManagerReviewConfirmation
    | HrReviewConfirmation
)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    message: str = Field(min_length=1, max_length=2000)
    message_request_id: UUID | None = None
    operation_id: UUID | None = None
    task_action: TaskAction = "message"
    task_revision: TaskRevision | None = None

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value

    @model_validator(mode="after")
    def protocol_ids_must_be_distinct(self) -> "ChatRequest":
        identifiers = [
            identifier
            for identifier in (
                self.thread_id,
                self.message_request_id,
                self.operation_id,
            )
            if identifier is not None
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("thread_id, message_request_id, and operation_id must differ")
        return self


class ChatResponse(BaseModel):
    success: bool
    thread_id: UUID
    intent: str
    response: str
    data: dict[str, Any] | list[dict[str, Any]] | None = None
    message_request_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    operation_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    task_status: TaskStatus | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    task_revision: TaskRevision | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    requires_confirmation: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    operation_status: OperationStatus | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    code: str | None = Field(default=None, exclude_if=lambda value: value is None)
    replayed: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    confirmation: ChatConfirmation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
