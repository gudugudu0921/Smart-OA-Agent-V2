"""Read-only audit log response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogResponse(BaseModel):
    id: int
    user_id: int | None
    employee_no: str | None = None
    name: str | None = None
    action: str
    resource_type: str
    resource_id: str | None
    detail: dict[str, Any] | None
    ip_address: str | None
    request_id: str | None = None
    created_at: datetime
