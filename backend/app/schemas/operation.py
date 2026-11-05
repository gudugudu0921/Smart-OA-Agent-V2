"""Safe API schemas for actor-owned business operation status."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class OperationStatusResponse(BaseModel):
    operation_id: str
    operation_type: Literal[
        "SUBMIT_LEAVE", "CANCEL_LEAVE", "MANAGER_REVIEW", "HR_REVIEW"
    ]
    status: Literal["PROCESSING", "SUCCEEDED", "FAILED", "UNKNOWN"]
    resource_id: str | None = None
    result_payload: dict[str, str] | None = None
    error_code: str | None = None
