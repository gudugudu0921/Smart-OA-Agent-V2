"""Leave application request and response schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


LeaveType = Literal[
    "annual",
    "sick",
    "personal",
    "marriage",
    "maternity",
    "bereavement",
    "other",
]
ReviewDecision = Literal["approved", "rejected"]


class LeaveCreate(BaseModel):
    leave_type: LeaveType
    start_date: date
    end_date: date
    reason: str | None = None


class LeaveReviewRequest(BaseModel):
    decision: ReviewDecision
    comment: str | None = None


class LeaveResponse(BaseModel):
    id: str
    employee_id: int
    leave_type: str
    start_date: date
    end_date: date
    leave_days: float
    reason: str | None
    status: str
    manager_status: str
    manager_id: int | None
    manager_comment: str | None
    manager_approved_at: datetime | None
    hr_status: str
    hr_id: int | None
    hr_comment: str | None
    hr_approved_at: datetime | None
    submitted_at: datetime
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
