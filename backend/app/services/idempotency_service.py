"""Actor-bound idempotency record primitives without business execution."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.business_operation import (
    BUSINESS_OPERATION_STATUSES,
    BUSINESS_OPERATION_TYPES,
    BusinessOperation,
    utc_now_naive,
)


FINGERPRINT_VERSION = 1
MAX_RESULT_PAYLOAD_BYTES = 64 * 1024
SAFE_OPERATION_ERROR_CODES = frozenset(
    {
        "LEAVE_VALIDATION_ERROR",
        "LEAVE_CONFLICT",
        "LEAVE_NOT_FOUND",
        "LEAVE_PERMISSION_DENIED",
        "EXECUTION_ABORTED",
        "COMMIT_RESULT_UNKNOWN",
    }
)


class IdempotencyRecordError(Exception):
    """Base class for expected idempotency-record failures."""


class InvalidIdempotencyInputError(IdempotencyRecordError):
    """Raised when trusted application code supplies invalid record input."""


class IdempotencyConflictError(IdempotencyRecordError):
    """Raised when an operation key is reused for a different request."""


class OperationNotFoundError(IdempotencyRecordError):
    """Hide both missing records and records owned by another actor."""


class OperationInProgressError(IdempotencyRecordError):
    """Raised when a live PROCESSING operation must not execute twice."""


class OperationPreviouslyFailedError(IdempotencyRecordError):
    """Raised when a FAILED operation cannot be replayed as a success."""


class OperationResultUnknownError(IdempotencyRecordError):
    """Raised when the caller must not guess whether an operation committed."""


def normalize_uuid(value: str | UUID, *, field_name: str) -> str:
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidIdempotencyInputError(f"{field_name} must be a UUID") from exc


def normalize_operation_type(value: str | Enum) -> str:
    normalized = value.value if isinstance(value, Enum) else value
    if normalized not in BUSINESS_OPERATION_TYPES:
        raise InvalidIdempotencyInputError("Unsupported operation_type")
    return str(normalized)


def normalize_status(value: str | Enum) -> str:
    normalized = value.value if isinstance(value, Enum) else value
    if normalized not in BUSINESS_OPERATION_STATUSES:
        raise InvalidIdempotencyInputError("Unsupported operation status")
    return str(normalized)


def _normalize_payload_value(value: Any, *, field_name: str | None = None) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if field_name and field_name.endswith("_id"):
            try:
                return str(UUID(value))
            except ValueError:
                pass
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidIdempotencyInputError("Non-finite floats are not supported")
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidIdempotencyInputError(
                "Datetime payload values must include a timezone"
            )
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _normalize_payload_value(value.value, field_name=field_name)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidIdempotencyInputError("Payload keys must be strings")
            normalized[key] = _normalize_payload_value(item, field_name=key)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize_payload_value(item, field_name=field_name) for item in value
        ]
    raise InvalidIdempotencyInputError(
        f"Unsupported payload value type: {type(value).__name__}"
    )


def canonical_payload_json(operation_type: str | Enum, payload: Mapping[str, Any]) -> str:
    """Return version-stable canonical JSON with the operation type bound in."""
    if not isinstance(payload, Mapping):
        raise InvalidIdempotencyInputError("payload must be a mapping")
    envelope = {
        "operation_type": normalize_operation_type(operation_type),
        "payload": _normalize_payload_value(payload),
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def calculate_request_fingerprint(
    operation_type: str | Enum,
    payload: Mapping[str, Any],
) -> str:
    canonical = canonical_payload_json(operation_type, payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_actor_user_id(actor_user_id: int) -> int:
    if isinstance(actor_user_id, bool) or not isinstance(actor_user_id, int):
        raise InvalidIdempotencyInputError("actor_user_id must be an integer")
    if actor_user_id <= 0:
        raise InvalidIdempotencyInputError("actor_user_id must be positive")
    return actor_user_id


def _normalize_optional_uuid(value: str | UUID | None, field_name: str) -> str | None:
    return None if value is None else normalize_uuid(value, field_name=field_name)


def _validate_existing_record(
    operation: BusinessOperation,
    *,
    actor_user_id: int,
    operation_type: str,
    request_fingerprint: str,
) -> BusinessOperation:
    if operation.actor_user_id != actor_user_id:
        raise OperationNotFoundError("Operation not found")
    if (
        operation.operation_type != operation_type
        or operation.fingerprint_version != FINGERPRINT_VERSION
        or operation.request_fingerprint != request_fingerprint
    ):
        raise IdempotencyConflictError(
            "operation_id is already bound to a different request"
        )
    return operation


def get_operation_for_actor(
    db: Session,
    operation_id: str | UUID,
    actor_user_id: int,
) -> BusinessOperation:
    """Return only records owned by the authenticated actor."""
    normalized_id = normalize_uuid(operation_id, field_name="operation_id")
    actor_id = _validate_actor_user_id(actor_user_id)
    operation = (
        db.query(BusinessOperation)
        .filter(
            BusinessOperation.operation_id == normalized_id,
            BusinessOperation.actor_user_id == actor_id,
        )
        .first()
    )
    if operation is None:
        raise OperationNotFoundError("Operation not found")
    return operation


def get_safe_operation_status(
    db: Session,
    *,
    operation_id: str | UUID,
    actor_user_id: int,
) -> dict[str, Any]:
    """Return a strict public projection for one actor-owned leave operation."""
    operation = get_operation_for_actor(db, operation_id, actor_user_id)
    if operation.operation_type not in {
        "SUBMIT_LEAVE",
        "CANCEL_LEAVE",
        "MANAGER_REVIEW",
        "HR_REVIEW",
    }:
        raise OperationNotFoundError("Operation not found")
    if operation.resource_type not in {None, "leave_application"}:
        raise OperationResultUnknownError("Operation result is not safely replayable")

    safe_result: dict[str, str] | None = None
    if operation.status == "SUCCEEDED":
        raw_result = operation.result_payload
        application_id = (
            raw_result.get("application_id") if isinstance(raw_result, dict) else None
        )
        try:
            normalized_resource_id = normalize_uuid(
                operation.resource_id,
                field_name="resource_id",
            )
            normalized_application_id = normalize_uuid(
                application_id,
                field_name="application_id",
            )
        except InvalidIdempotencyInputError as error:
            raise OperationResultUnknownError(
                "Operation result is not safely replayable"
            ) from error
        if (
            operation.resource_type != "leave_application"
            or normalized_resource_id != normalized_application_id
        ):
            raise OperationResultUnknownError(
                "Operation result is not safely replayable"
            )
        safe_result = {"application_id": normalized_application_id}

    safe_error_code = (
        operation.error_code
        if operation.error_code in SAFE_OPERATION_ERROR_CODES
        else None
    )
    return {
        "operation_id": operation.operation_id,
        "operation_type": operation.operation_type,
        "status": operation.status,
        "resource_id": operation.resource_id,
        "result_payload": safe_result,
        "error_code": safe_error_code,
    }


def lock_operation_for_actor(
    db: Session,
    *,
    operation_id: str | UUID,
    actor_user_id: int,
    operation_type: str | Enum,
    payload: Mapping[str, Any],
) -> BusinessOperation:
    """Lock and validate an actor-bound operation inside a business transaction."""
    normalized_id = normalize_uuid(operation_id, field_name="operation_id")
    actor_id = _validate_actor_user_id(actor_user_id)
    normalized_type = normalize_operation_type(operation_type)
    request_fingerprint = calculate_request_fingerprint(normalized_type, payload)
    operation = (
        db.query(BusinessOperation)
        .filter(
            BusinessOperation.operation_id == normalized_id,
            BusinessOperation.actor_user_id == actor_id,
        )
        .with_for_update()
        .first()
    )
    if operation is None:
        raise OperationNotFoundError("Operation not found")
    return _validate_existing_record(
        operation,
        actor_user_id=actor_id,
        operation_type=normalized_type,
        request_fingerprint=request_fingerprint,
    )


def get_or_create_operation_record(
    db: Session,
    *,
    operation_id: str | UUID,
    actor_user_id: int,
    operation_type: str | Enum,
    payload: Mapping[str, Any],
    thread_id: str | UUID | None = None,
    first_request_id: str | None = None,
    lease_owner: str | UUID | None = None,
    lease_expires_at: datetime | None = None,
) -> tuple[BusinessOperation, bool]:
    """Create or validate one record; caller owns commit and business execution.

    This foundation helper performs no leave mutation, lease takeover, or retry. On
    a unique-key race it rolls back the current Session before reading the winner,
    so it must be used in a dedicated record-reservation transaction.
    """
    normalized_id = normalize_uuid(operation_id, field_name="operation_id")
    actor_id = _validate_actor_user_id(actor_user_id)
    normalized_type = normalize_operation_type(operation_type)
    request_fingerprint = calculate_request_fingerprint(normalized_type, payload)

    existing = db.get(BusinessOperation, normalized_id)
    if existing is not None:
        return (
            _validate_existing_record(
                existing,
                actor_user_id=actor_id,
                operation_type=normalized_type,
                request_fingerprint=request_fingerprint,
            ),
            False,
        )

    if lease_expires_at is not None:
        if lease_expires_at.tzinfo is None or lease_expires_at.utcoffset() is None:
            raise InvalidIdempotencyInputError(
                "lease_expires_at must include a timezone"
            )
        normalized_lease_expires_at = lease_expires_at.astimezone(UTC).replace(
            tzinfo=None
        )
    else:
        normalized_lease_expires_at = None

    operation = BusinessOperation(
        operation_id=normalized_id,
        actor_user_id=actor_id,
        operation_type=normalized_type,
        fingerprint_version=FINGERPRINT_VERSION,
        request_fingerprint=request_fingerprint,
        status="PROCESSING",
        lease_owner=_normalize_optional_uuid(lease_owner, "lease_owner"),
        lease_expires_at=normalized_lease_expires_at,
        thread_id=_normalize_optional_uuid(thread_id, "thread_id"),
        first_request_id=(first_request_id[:64] if first_request_id else None),
    )
    db.add(operation)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.get(BusinessOperation, normalized_id)
        if winner is None:
            raise
        return (
            _validate_existing_record(
                winner,
                actor_user_id=actor_id,
                operation_type=normalized_type,
                request_fingerprint=request_fingerprint,
            ),
            False,
        )
    return operation, True


def set_operation_status(
    operation: BusinessOperation,
    status: str | Enum,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    result_payload: Mapping[str, Any] | Sequence[Any] | None = None,
    error_code: str | None = None,
) -> BusinessOperation:
    """Update record fields only; no transition, lease, commit, or retry policy."""
    normalized_status = normalize_status(status)
    if result_payload is not None:
        normalized_result = _normalize_payload_value(result_payload)
        encoded_result = json.dumps(
            normalized_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded_result) > MAX_RESULT_PAYLOAD_BYTES:
            raise InvalidIdempotencyInputError("result_payload exceeds 64 KiB")
    else:
        normalized_result = None
    if error_code is not None and len(error_code) > 64:
        raise InvalidIdempotencyInputError("error_code is too long")

    operation.status = normalized_status
    operation.resource_type = resource_type
    operation.resource_id = resource_id
    operation.result_payload = normalized_result
    operation.error_code = error_code
    operation.updated_at = utc_now_naive()
    operation.completed_at = (
        utc_now_naive() if normalized_status in {"SUCCEEDED", "FAILED", "UNKNOWN"} else None
    )
    return operation


def transition_processing_operation(
    operation: BusinessOperation,
    status: str | Enum,
    **result_fields: Any,
) -> BusinessOperation:
    """Finish a locked PROCESSING record without overwriting a terminal result."""
    normalized_status = normalize_status(status)
    if normalized_status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
        raise InvalidIdempotencyInputError(
            "PROCESSING can only transition to a terminal status"
        )
    if operation.status != "PROCESSING":
        raise IdempotencyConflictError(
            "Only a PROCESSING operation can transition to a terminal status"
        )
    return set_operation_status(operation, normalized_status, **result_fields)
