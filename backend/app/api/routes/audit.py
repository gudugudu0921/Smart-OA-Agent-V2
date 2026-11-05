"""Admin-only read access to audit logs."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.dependencies import require_roles
from app.db.mysql import get_db
from app.models.user import User
from app.schemas.audit import AuditLogResponse
from app.services.audit_service import get_audit_logs


router = APIRouter()


@router.get("/logs", response_model=list[AuditLogResponse])
def read_audit_logs(
    action: str | None = None,
    resource_type: str | None = None,
    user_id: int | None = Query(default=None, ge=1),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_roles("admin")),
) -> list[AuditLogResponse]:
    return get_audit_logs(
        db,
        action=action,
        resource_type=resource_type,
        user_id=user_id,
        skip=skip,
        limit=limit,
    )
