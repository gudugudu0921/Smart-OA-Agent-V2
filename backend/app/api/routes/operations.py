"""Actor-owned, read-only business operation status API."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_business_user
from app.core.exceptions import AppException
from app.db.mysql import get_db
from app.models.user import User
from app.schemas.operation import OperationStatusResponse
from app.services.idempotency_service import (
    IdempotencyRecordError,
    OperationNotFoundError,
    OperationResultUnknownError,
    get_safe_operation_status,
)


router = APIRouter()


@router.get("/{operation_id}", response_model=OperationStatusResponse)
def read_operation_status(
    operation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_business_user),
) -> OperationStatusResponse:
    try:
        result = get_safe_operation_status(
            db,
            operation_id=operation_id,
            actor_user_id=current_user.id,
        )
    except OperationNotFoundError as error:
        raise AppException(
            "OPERATION_NOT_FOUND",
            "操作记录不存在。",
            404,
        ) from error
    except OperationResultUnknownError as error:
        raise AppException(
            "OPERATION_RESULT_UNKNOWN",
            "该操作结果暂时无法确认，请勿重复提交。",
            409,
        ) from error
    except IdempotencyRecordError as error:
        raise AppException(
            "IDEMPOTENCY_ERROR",
            "操作状态无法安全读取。",
            409,
        ) from error
    return OperationStatusResponse.model_validate(result)
