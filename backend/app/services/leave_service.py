"""Traditional OA leave workflow and query operations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Query, Session

from app.models.leave_application import LeaveApplication
from app.models.user import User
from app.schemas.leave import LeaveCreate, LeaveReviewRequest
from app.services.audit_service import create_audit_log
from app.services.idempotency_service import (
    IdempotencyConflictError,
    IdempotencyRecordError,
    OperationInProgressError,
    OperationPreviouslyFailedError,
    OperationResultUnknownError,
    get_or_create_operation_record,
    lock_operation_for_actor,
    transition_processing_operation,
)
from app.services.notification_service import (
    notify_hr_reviewed,
    notify_leave_created,
    notify_manager_reviewed,
)


ACTIVE_LEAVE_STATUSES = ("pending", "approved")


class LeaveServiceError(Exception):
    """Base exception for expected leave workflow errors."""


class LeaveValidationError(LeaveServiceError):
    """Raised when submitted business data is invalid."""


class LeaveConflictError(LeaveServiceError):
    """Raised when the application state conflicts with an operation."""


class LeaveNotFoundError(LeaveServiceError):
    """Raised for missing or deliberately hidden leave applications."""


class LeavePermissionError(LeaveServiceError):
    """Raised when a trusted caller is not allowed to perform a leave action."""


def _validate_date_range(start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise LeaveValidationError("start_date must be on or before end_date")


def _apply_date_filters(
    query: Query,
    start_date: date | None,
    end_date: date | None,
) -> Query:
    if start_date is not None and end_date is not None:
        _validate_date_range(start_date, end_date)
    if start_date is not None:
        query = query.filter(LeaveApplication.end_date >= start_date)
    if end_date is not None:
        query = query.filter(LeaveApplication.start_date <= end_date)
    return query


def _commit_and_refresh(db: Session, application: LeaveApplication) -> LeaveApplication:
    try:
        db.commit()
        db.refresh(application)
    except Exception:
        db.rollback()
        raise
    return application


def create_leave_application_mutation(
    db: Session,
    employee: User,
    payload: LeaveCreate,
    ip_address: str | None = None,
) -> LeaveApplication:
    """Add a leave application, audit, and notification without committing."""
    _validate_date_range(payload.start_date, payload.end_date)
    if payload.start_date < date.today():
        raise LeaveValidationError("start_date cannot be in the past")
    if employee.manager_id is None:
        raise LeaveValidationError("Employee does not have a direct manager")

    # Serialize submissions for one employee so concurrent requests cannot both
    # pass overlap validation before either transaction commits.
    db.query(User).filter(User.id == employee.id).with_for_update().one()

    overlap = (
        db.query(LeaveApplication.id)
        .filter(
            LeaveApplication.employee_id == employee.id,
            LeaveApplication.status.in_(ACTIVE_LEAVE_STATUSES),
            LeaveApplication.start_date <= payload.end_date,
            LeaveApplication.end_date >= payload.start_date,
        )
        .first()
    )
    if overlap is not None:
        raise LeaveConflictError(
            "Leave dates overlap an existing pending or approved application"
        )

    now = datetime.now()
    leave_days = (payload.end_date - payload.start_date).days + 1
    application = LeaveApplication(
        id=str(uuid4()),
        employee_id=employee.id,
        leave_type=payload.leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        leave_days=Decimal(f"{leave_days}.0"),
        reason=payload.reason,
        status="pending",
        manager_status="pending",
        manager_id=employee.manager_id,
        hr_status="pending",
        submitted_at=now,
    )
    db.add(application)
    create_audit_log(
        db=db,
        user_id=employee.id,
        action="CREATE_LEAVE",
        resource_type="leave_application",
        resource_id=application.id,
        detail={
            "old_status": None,
            "new_status": "pending",
            "leave_type": payload.leave_type,
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
        },
        ip_address=ip_address,
    )
    notify_leave_created(db, application, employee)
    return application


def create_leave_application(
    db: Session,
    employee: User,
    payload: LeaveCreate,
    ip_address: str | None = None,
) -> LeaveApplication:
    """Compatibility wrapper preserving the original commit and response behavior."""
    application = create_leave_application_mutation(
        db=db,
        employee=employee,
        payload=payload,
        ip_address=ip_address,
    )
    return _commit_and_refresh(db, application)


SessionFactory = Callable[[], Session]
SUBMIT_LEAVE_OPERATION = "SUBMIT_LEAVE"
CANCEL_LEAVE_OPERATION = "CANCEL_LEAVE"
MANAGER_REVIEW_OPERATION = "MANAGER_REVIEW"
HR_REVIEW_OPERATION = "HR_REVIEW"


def _leave_create_fingerprint_payload(payload: LeaveCreate) -> dict[str, Any]:
    return payload.model_dump(mode="python")


def _rollback_safely(db: Session) -> bool:
    try:
        db.rollback()
    except Exception:
        return False
    return True


def _commit_idempotent_transaction(db: Session) -> None:
    """Small seam for commit-outcome integration tests; never retries writes."""
    db.commit()


def _before_idempotent_commit() -> None:
    """No-op seam for failures known to occur before commit is attempted."""


def _load_succeeded_leave(
    db: Session,
    operation: Any,
    actor_user_id: int,
) -> LeaveApplication:
    result = operation.result_payload
    result_id = result.get("application_id") if isinstance(result, dict) else None
    application_id = operation.resource_id
    if (
        operation.resource_type != "leave_application"
        or not application_id
        or result_id != application_id
    ):
        raise OperationResultUnknownError(
            "Succeeded operation does not contain a replayable leave result"
        )
    query = db.query(LeaveApplication).filter(LeaveApplication.id == application_id)
    if operation.operation_type in {
        SUBMIT_LEAVE_OPERATION,
        CANCEL_LEAVE_OPERATION,
    }:
        query = query.filter(LeaveApplication.employee_id == actor_user_id)
    elif operation.operation_type == MANAGER_REVIEW_OPERATION:
        query = query.filter(LeaveApplication.manager_id == actor_user_id)
    elif operation.operation_type == HR_REVIEW_OPERATION:
        query = query.filter(LeaveApplication.hr_id == actor_user_id)
    else:
        raise OperationResultUnknownError(
            "Succeeded operation type is not replayable as a leave result"
        )
    application = query.first()
    if application is None:
        raise OperationResultUnknownError(
            "Succeeded operation result is not available for replay"
        )
    return application


def _return_or_reject_existing_operation(
    session_factory: SessionFactory,
    *,
    operation_id: str,
    actor_user_id: int,
    operation_type: str,
    payload_data: dict[str, Any],
) -> LeaveApplication:
    db = session_factory()
    try:
        operation = lock_operation_for_actor(
            db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=operation_type,
            payload=payload_data,
        )
        if operation.status == "SUCCEEDED":
            application = _load_succeeded_leave(db, operation, actor_user_id)
            db.expunge(application)
            return application
        if operation.status == "PROCESSING":
            raise OperationInProgressError("Operation is already processing")
        if operation.status == "FAILED":
            raise OperationPreviouslyFailedError("Operation previously failed")
        raise OperationResultUnknownError("Operation result is unknown")
    finally:
        db.close()


def _record_operation_terminal_state(
    session_factory: SessionFactory,
    *,
    operation_id: str,
    actor_user_id: int,
    operation_type: str,
    payload_data: dict[str, Any],
    status: str,
    error_code: str,
) -> None:
    db = session_factory()
    try:
        operation = lock_operation_for_actor(
            db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=operation_type,
            payload=payload_data,
        )
        if operation.status == "PROCESSING":
            transition_processing_operation(
                operation,
                status,
                error_code=error_code,
            )
            db.commit()
        elif operation.status != "SUCCEEDED" and operation.status != status:
            raise IdempotencyConflictError(
                "Operation is already in a different terminal state"
            )
    except Exception:
        _rollback_safely(db)
        raise
    finally:
        db.close()


def _resolve_uncertain_commit(
    session_factory: SessionFactory,
    *,
    operation_id: str,
    actor_user_id: int,
    operation_type: str,
    payload_data: dict[str, Any],
    commit_error: Exception,
) -> LeaveApplication:
    db = session_factory()
    try:
        operation = lock_operation_for_actor(
            db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=operation_type,
            payload=payload_data,
        )
        if operation.status == "SUCCEEDED":
            application = _load_succeeded_leave(db, operation, actor_user_id)
            db.expunge(application)
            return application
        if operation.status == "PROCESSING":
            transition_processing_operation(
                operation,
                "UNKNOWN",
                error_code="COMMIT_RESULT_UNKNOWN",
            )
            db.commit()
        raise OperationResultUnknownError("Operation commit result is unknown") from commit_error
    except OperationResultUnknownError:
        raise
    except Exception as verification_error:
        _rollback_safely(db)
        raise OperationResultUnknownError(
            "Operation commit result could not be verified"
        ) from verification_error
    finally:
        db.close()


def create_leave_application_idempotent(
    session_factory: SessionFactory,
    employee: User,
    payload: LeaveCreate,
    operation_id: str,
    ip_address: str | None = None,
) -> LeaveApplication:
    """Create leave once using separate reservation and atomic business transactions.

    ``operation_id`` is supplied by trusted backend orchestration. REST callers may
    provide the key through the validated Idempotency-Key header; it is never an
    identity source and remains absent from Chat and LLM-visible tool schemas.
    """
    if employee.role != "employee":
        raise LeavePermissionError("Only employees can create leave applications")
    actor_user_id = employee.id
    payload_data = _leave_create_fingerprint_payload(payload)

    # Transaction A owns only the durable reservation. The foundation helper may
    # roll back on a unique-key race, so no business mutation may share this Session.
    reservation_db = session_factory()
    try:
        operation, created = get_or_create_operation_record(
            reservation_db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=SUBMIT_LEAVE_OPERATION,
            payload=payload_data,
        )
        reservation_status = operation.status
        reservation_db.commit()
    except IdempotencyRecordError:
        _rollback_safely(reservation_db)
        raise
    except Exception as reservation_error:
        _rollback_safely(reservation_db)
        raise OperationResultUnknownError(
            "Operation reservation result could not be confirmed"
        ) from reservation_error
    finally:
        reservation_db.close()

    if not created:
        return _return_or_reject_existing_operation(
            session_factory,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=SUBMIT_LEAVE_OPERATION,
            payload_data=payload_data,
        )
    if reservation_status != "PROCESSING":
        raise IdempotencyConflictError("New reservation is not PROCESSING")

    # Transaction B locks the operation and employee rows, then atomically commits
    # the leave, audit, notification, replay result, and SUCCEEDED state.
    db = session_factory()
    try:
        operation = lock_operation_for_actor(
            db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=SUBMIT_LEAVE_OPERATION,
            payload=payload_data,
        )
        if operation.status != "PROCESSING":
            raise IdempotencyConflictError("Reserved operation is not PROCESSING")
        application = create_leave_application_mutation(
            db=db,
            employee=employee,
            payload=payload,
            ip_address=ip_address,
        )
        db.flush()
        db.refresh(application)
        transition_processing_operation(
            operation,
            "SUCCEEDED",
            resource_type="leave_application",
            resource_id=application.id,
            result_payload={"application_id": application.id},
        )
        db.flush()
        # Keep the return value usable after Session close without a post-commit
        # refresh that could misreport an already committed operation as failed.
        db.expunge(application)
        _before_idempotent_commit()
    except (LeaveValidationError, LeaveConflictError, LeavePermissionError) as error:
        if not _rollback_safely(db):
            db.close()
            raise OperationResultUnknownError(
                "Operation rollback could not be confirmed"
            ) from error
        db.close()
        error_code = {
            LeaveValidationError: "LEAVE_VALIDATION_ERROR",
            LeaveConflictError: "LEAVE_CONFLICT",
            LeavePermissionError: "LEAVE_PERMISSION_DENIED",
        }[type(error)]
        try:
            _record_operation_terminal_state(
                session_factory,
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                operation_type=SUBMIT_LEAVE_OPERATION,
                payload_data=payload_data,
                status="FAILED",
                error_code=error_code,
            )
        except Exception as state_error:
            raise OperationResultUnknownError(
                "Operation failure state could not be persisted"
            ) from state_error
        raise
    except Exception as error:
        if not _rollback_safely(db):
            db.close()
            raise OperationResultUnknownError(
                "Operation rollback could not be confirmed"
            ) from error
        db.close()
        try:
            _record_operation_terminal_state(
                session_factory,
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                operation_type=SUBMIT_LEAVE_OPERATION,
                payload_data=payload_data,
                status="FAILED",
                error_code="EXECUTION_ABORTED",
            )
        except Exception as state_error:
            raise OperationResultUnknownError(
                "Operation failure state could not be persisted"
            ) from state_error
        raise

    try:
        _commit_idempotent_transaction(db)
    except Exception as commit_error:
        _rollback_safely(db)
        db.close()
        return _resolve_uncertain_commit(
            session_factory,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=SUBMIT_LEAVE_OPERATION,
            payload_data=payload_data,
            commit_error=commit_error,
        )
    db.close()
    return application


def get_my_leave_applications(
    db: Session,
    employee_id: int,
    status: str | None = None,
    leave_type: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[LeaveApplication]:
    query = db.query(LeaveApplication).filter(
        LeaveApplication.employee_id == employee_id
    )
    if status is not None:
        query = query.filter(LeaveApplication.status == status)
    if leave_type is not None:
        query = query.filter(LeaveApplication.leave_type == leave_type)
    query = _apply_date_filters(query, start_date, end_date)
    return (
        query.order_by(LeaveApplication.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_leave_application(
    db: Session,
    application_id: str,
    current_user: User,
) -> LeaveApplication:
    application = (
        db.query(LeaveApplication)
        .filter(LeaveApplication.id == application_id)
        .first()
    )
    if application is None:
        raise LeaveNotFoundError("Leave application not found")

    can_view = (
        (current_user.role == "employee" and application.employee_id == current_user.id)
        or (current_user.role == "manager" and application.manager_id == current_user.id)
        or current_user.role in {"hr", "admin"}
    )
    if not can_view:
        raise LeaveNotFoundError("Leave application not found")
    return application


def get_cancellable_leave_application(
    db: Session,
    application_id: str,
    employee: User,
) -> LeaveApplication:
    """Read an owned application and verify that it is currently cancellable."""
    application = (
        db.query(LeaveApplication)
        .filter(
            LeaveApplication.id == application_id,
            LeaveApplication.employee_id == employee.id,
        )
        .first()
    )
    if application is None:
        raise LeaveNotFoundError("Leave application not found")
    if application.status != "pending" or application.manager_status != "pending":
        raise LeaveConflictError("Only an unreviewed pending application can be cancelled")
    return application


def cancel_leave_application_mutation(
    db: Session,
    application_id: str,
    employee: User,
    ip_address: str | None = None,
) -> LeaveApplication:
    """Cancel one owned application without committing the current transaction."""
    application = (
        db.query(LeaveApplication)
        .filter(
            LeaveApplication.id == application_id,
            LeaveApplication.employee_id == employee.id,
        )
        .with_for_update()
        .first()
    )
    if application is None:
        raise LeaveNotFoundError("Leave application not found")
    if application.status != "pending" or application.manager_status != "pending":
        raise LeaveConflictError("Only an unreviewed pending application can be cancelled")

    old_status = application.status
    application.status = "cancelled"
    application.cancelled_at = datetime.now()
    create_audit_log(
        db=db,
        user_id=employee.id,
        action="CANCEL_LEAVE",
        resource_type="leave_application",
        resource_id=application.id,
        detail={"old_status": old_status, "new_status": "cancelled"},
        ip_address=ip_address,
    )
    return application


def cancel_leave_application(
    db: Session,
    application_id: str,
    employee: User,
    ip_address: str | None = None,
) -> LeaveApplication:
    application = cancel_leave_application_mutation(
        db=db,
        application_id=application_id,
        employee=employee,
        ip_address=ip_address,
    )
    return _commit_and_refresh(db, application)


def cancel_leave_application_idempotent(
    session_factory: SessionFactory,
    employee: User,
    application_id: str,
    operation_id: str,
    confirmed_payload: dict[str, Any],
    ip_address: str | None = None,
) -> tuple[LeaveApplication, bool]:
    """Cancel one leave once and replay its actor-bound terminal result safely."""
    if employee.role != "employee":
        raise LeavePermissionError("Only employees can cancel leave applications")
    payload_data = dict(confirmed_payload)
    if payload_data.get("application_id") != application_id:
        raise IdempotencyConflictError(
            "Confirmed cancellation target does not match tool arguments"
        )
    actor_user_id = employee.id

    reservation_db = session_factory()
    try:
        operation, created = get_or_create_operation_record(
            reservation_db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=CANCEL_LEAVE_OPERATION,
            payload=payload_data,
        )
        reservation_status = operation.status
        reservation_db.commit()
    except IdempotencyRecordError:
        _rollback_safely(reservation_db)
        raise
    except Exception as reservation_error:
        _rollback_safely(reservation_db)
        raise OperationResultUnknownError(
            "Cancellation reservation result could not be confirmed"
        ) from reservation_error
    finally:
        reservation_db.close()

    if not created:
        application = _return_or_reject_existing_operation(
            session_factory,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=CANCEL_LEAVE_OPERATION,
            payload_data=payload_data,
        )
        if application.status != "cancelled":
            raise OperationResultUnknownError(
                "Cancellation replay resource is not cancelled"
            )
        return application, True
    if reservation_status != "PROCESSING":
        raise IdempotencyConflictError("New cancellation reservation is not PROCESSING")

    db = session_factory()
    try:
        operation = lock_operation_for_actor(
            db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=CANCEL_LEAVE_OPERATION,
            payload=payload_data,
        )
        if operation.status != "PROCESSING":
            raise IdempotencyConflictError(
                "Reserved cancellation operation is not PROCESSING"
            )
        application = cancel_leave_application_mutation(
            db=db,
            application_id=application_id,
            employee=employee,
            ip_address=ip_address,
        )
        db.flush()
        db.refresh(application)
        transition_processing_operation(
            operation,
            "SUCCEEDED",
            resource_type="leave_application",
            resource_id=application.id,
            result_payload={"application_id": application.id},
        )
        db.flush()
        db.expunge(application)
        _before_idempotent_commit()
    except (
        LeaveValidationError,
        LeaveConflictError,
        LeaveNotFoundError,
        LeavePermissionError,
    ) as error:
        if not _rollback_safely(db):
            db.close()
            raise OperationResultUnknownError(
                "Cancellation rollback could not be confirmed"
            ) from error
        db.close()
        error_code = {
            LeaveValidationError: "LEAVE_VALIDATION_ERROR",
            LeaveConflictError: "LEAVE_CONFLICT",
            LeaveNotFoundError: "LEAVE_NOT_FOUND",
            LeavePermissionError: "LEAVE_PERMISSION_DENIED",
        }[type(error)]
        try:
            _record_operation_terminal_state(
                session_factory,
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                operation_type=CANCEL_LEAVE_OPERATION,
                payload_data=payload_data,
                status="FAILED",
                error_code=error_code,
            )
        except Exception as state_error:
            raise OperationResultUnknownError(
                "Cancellation failure state could not be persisted"
            ) from state_error
        raise
    except Exception as error:
        if not _rollback_safely(db):
            db.close()
            raise OperationResultUnknownError(
                "Cancellation rollback could not be confirmed"
            ) from error
        db.close()
        try:
            _record_operation_terminal_state(
                session_factory,
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                operation_type=CANCEL_LEAVE_OPERATION,
                payload_data=payload_data,
                status="FAILED",
                error_code="EXECUTION_ABORTED",
            )
        except Exception as state_error:
            raise OperationResultUnknownError(
                "Cancellation failure state could not be persisted"
            ) from state_error
        raise

    try:
        _commit_idempotent_transaction(db)
    except Exception as commit_error:
        _rollback_safely(db)
        db.close()
        application = _resolve_uncertain_commit(
            session_factory,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=CANCEL_LEAVE_OPERATION,
            payload_data=payload_data,
            commit_error=commit_error,
        )
        if application.status != "cancelled":
            raise OperationResultUnknownError(
                "Cancellation commit result is not safely replayable"
            )
        return application, True
    db.close()
    return application, False


def get_manager_pending_applications(
    db: Session,
    manager_id: int,
    skip: int = 0,
    limit: int = 100,
) -> list[LeaveApplication]:
    return (
        db.query(LeaveApplication)
        .filter(
            LeaveApplication.manager_id == manager_id,
            LeaveApplication.manager_status == "pending",
            LeaveApplication.status == "pending",
        )
        .order_by(LeaveApplication.submitted_at.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_manager_reviewable_leave_application(
    db: Session,
    application_id: str,
    manager: User,
) -> LeaveApplication:
    """Read one assigned, currently reviewable application without mutation."""
    if manager.role != "manager":
        raise LeavePermissionError("Only managers can review leave applications")
    application = (
        db.query(LeaveApplication)
        .filter(
            LeaveApplication.id == application_id,
            LeaveApplication.manager_id == manager.id,
        )
        .first()
    )
    if application is None:
        raise LeaveNotFoundError("Leave application not found")
    if application.manager_status != "pending" or application.status != "pending":
        raise LeaveConflictError("Leave application has already been processed")
    return application


def review_leave_by_manager_mutation(
    db: Session,
    application_id: str,
    manager: User,
    review: LeaveReviewRequest,
    ip_address: str | None = None,
) -> LeaveApplication:
    """Apply one manager decision without committing the current transaction."""
    if manager.role != "manager":
        raise LeavePermissionError("Only managers can review leave applications")
    application = (
        db.query(LeaveApplication)
        .filter(
            LeaveApplication.id == application_id,
            LeaveApplication.manager_id == manager.id,
        )
        .with_for_update()
        .first()
    )
    if application is None:
        raise LeaveNotFoundError("Leave application not found")
    if application.manager_status != "pending" or application.status != "pending":
        raise LeaveConflictError("Leave application has already been processed")

    old_status = application.status
    application.manager_status = review.decision
    application.manager_comment = review.comment
    application.manager_approved_at = datetime.now()
    if review.decision == "rejected":
        application.status = "rejected"

    action = (
        "MANAGER_APPROVE_LEAVE"
        if review.decision == "approved"
        else "MANAGER_REJECT_LEAVE"
    )
    create_audit_log(
        db=db,
        user_id=manager.id,
        action=action,
        resource_type="leave_application",
        resource_id=application.id,
        detail={
            "old_status": old_status,
            "new_status": application.status,
            "old_manager_status": "pending",
            "new_manager_status": review.decision,
            "comment": review.comment,
        },
        ip_address=ip_address,
    )
    notify_manager_reviewed(db, application, review.comment)
    return application


def review_leave_by_manager(
    db: Session,
    application_id: str,
    manager: User,
    review: LeaveReviewRequest,
    ip_address: str | None = None,
) -> LeaveApplication:
    application = review_leave_by_manager_mutation(
        db=db,
        application_id=application_id,
        manager=manager,
        review=review,
        ip_address=ip_address,
    )
    return _commit_and_refresh(db, application)


def review_leave_by_manager_idempotent(
    session_factory: SessionFactory,
    manager: User,
    application_id: str,
    review: LeaveReviewRequest,
    operation_id: str,
    confirmed_payload: dict[str, Any],
    ip_address: str | None = None,
) -> tuple[LeaveApplication, bool]:
    """Apply one actor-bound manager decision once and safely replay its result."""
    if manager.role != "manager":
        raise LeavePermissionError("Only managers can review leave applications")
    payload_data = dict(confirmed_payload)
    if (
        payload_data.get("application_id") != application_id
        or payload_data.get("decision") != review.decision
        or payload_data.get("comment") != review.comment
    ):
        raise IdempotencyConflictError(
            "Confirmed manager review does not match tool arguments"
        )
    actor_user_id = manager.id

    reservation_db = session_factory()
    try:
        operation, created = get_or_create_operation_record(
            reservation_db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=MANAGER_REVIEW_OPERATION,
            payload=payload_data,
        )
        reservation_status = operation.status
        reservation_db.commit()
    except IdempotencyRecordError:
        _rollback_safely(reservation_db)
        raise
    except Exception as reservation_error:
        _rollback_safely(reservation_db)
        raise OperationResultUnknownError(
            "Manager review reservation result could not be confirmed"
        ) from reservation_error
    finally:
        reservation_db.close()

    if not created:
        application = _return_or_reject_existing_operation(
            session_factory,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=MANAGER_REVIEW_OPERATION,
            payload_data=payload_data,
        )
        expected_status = "pending" if review.decision == "approved" else "rejected"
        if (
            application.manager_status != review.decision
            or application.status != expected_status
        ):
            raise OperationResultUnknownError(
                "Manager review replay resource is inconsistent"
            )
        return application, True
    if reservation_status != "PROCESSING":
        raise IdempotencyConflictError(
            "New manager review reservation is not PROCESSING"
        )

    db = session_factory()
    try:
        operation = lock_operation_for_actor(
            db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=MANAGER_REVIEW_OPERATION,
            payload=payload_data,
        )
        if operation.status != "PROCESSING":
            raise IdempotencyConflictError(
                "Reserved manager review operation is not PROCESSING"
            )
        application = review_leave_by_manager_mutation(
            db=db,
            application_id=application_id,
            manager=manager,
            review=review,
            ip_address=ip_address,
        )
        db.flush()
        db.refresh(application)
        transition_processing_operation(
            operation,
            "SUCCEEDED",
            resource_type="leave_application",
            resource_id=application.id,
            result_payload={"application_id": application.id},
        )
        db.flush()
        db.expunge(application)
        _before_idempotent_commit()
    except (
        LeaveValidationError,
        LeaveConflictError,
        LeaveNotFoundError,
        LeavePermissionError,
    ) as error:
        if not _rollback_safely(db):
            db.close()
            raise OperationResultUnknownError(
                "Manager review rollback could not be confirmed"
            ) from error
        db.close()
        error_code = {
            LeaveValidationError: "LEAVE_VALIDATION_ERROR",
            LeaveConflictError: "LEAVE_CONFLICT",
            LeaveNotFoundError: "LEAVE_NOT_FOUND",
            LeavePermissionError: "LEAVE_PERMISSION_DENIED",
        }[type(error)]
        try:
            _record_operation_terminal_state(
                session_factory,
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                operation_type=MANAGER_REVIEW_OPERATION,
                payload_data=payload_data,
                status="FAILED",
                error_code=error_code,
            )
        except Exception as state_error:
            raise OperationResultUnknownError(
                "Manager review failure state could not be persisted"
            ) from state_error
        raise
    except Exception as error:
        if not _rollback_safely(db):
            db.close()
            raise OperationResultUnknownError(
                "Manager review rollback could not be confirmed"
            ) from error
        db.close()
        try:
            _record_operation_terminal_state(
                session_factory,
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                operation_type=MANAGER_REVIEW_OPERATION,
                payload_data=payload_data,
                status="FAILED",
                error_code="EXECUTION_ABORTED",
            )
        except Exception as state_error:
            raise OperationResultUnknownError(
                "Manager review failure state could not be persisted"
            ) from state_error
        raise

    try:
        _commit_idempotent_transaction(db)
    except Exception as commit_error:
        _rollback_safely(db)
        db.close()
        application = _resolve_uncertain_commit(
            session_factory,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=MANAGER_REVIEW_OPERATION,
            payload_data=payload_data,
            commit_error=commit_error,
        )
        expected_status = "pending" if review.decision == "approved" else "rejected"
        if (
            application.manager_status != review.decision
            or application.status != expected_status
        ):
            raise OperationResultUnknownError(
                "Manager review commit result is not safely replayable"
            )
        return application, True
    db.close()
    return application, False


def get_manager_team_history(
    db: Session,
    manager_id: int,
    employee_id: int | None = None,
    status: str | None = None,
    leave_type: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[LeaveApplication]:
    query = (
        db.query(LeaveApplication)
        .join(User, LeaveApplication.employee_id == User.id)
        .filter(User.manager_id == manager_id)
    )
    if employee_id is not None:
        query = query.filter(LeaveApplication.employee_id == employee_id)
    if status is not None:
        query = query.filter(LeaveApplication.status == status)
    if leave_type is not None:
        query = query.filter(LeaveApplication.leave_type == leave_type)
    query = _apply_date_filters(query, start_date, end_date)
    return (
        query.order_by(LeaveApplication.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_hr_pending_applications(
    db: Session,
    skip: int = 0,
    limit: int = 100,
) -> list[LeaveApplication]:
    return (
        db.query(LeaveApplication)
        .filter(
            LeaveApplication.manager_status == "approved",
            LeaveApplication.hr_status == "pending",
            LeaveApplication.status == "pending",
        )
        .order_by(LeaveApplication.manager_approved_at.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_hr_reviewable_leave_application(
    db: Session,
    application_id: str,
    hr_user: User,
) -> LeaveApplication:
    """Read one currently reviewable HR application without mutation."""
    if hr_user.role != "hr":
        raise LeavePermissionError("Only HR can review leave applications")
    application = (
        db.query(LeaveApplication)
        .filter(LeaveApplication.id == application_id)
        .first()
    )
    if application is None:
        raise LeaveNotFoundError("Leave application not found")
    if (
        application.manager_status != "approved"
        or application.hr_status != "pending"
        or application.status != "pending"
    ):
        raise LeaveConflictError("Leave application is not awaiting HR review")
    return application


def review_leave_by_hr_mutation(
    db: Session,
    application_id: str,
    hr_user: User,
    review: LeaveReviewRequest,
    ip_address: str | None = None,
) -> LeaveApplication:
    """Apply one HR decision without committing the current transaction."""
    if hr_user.role != "hr":
        raise LeavePermissionError("Only HR can review leave applications")
    application = (
        db.query(LeaveApplication)
        .filter(LeaveApplication.id == application_id)
        .with_for_update()
        .first()
    )
    if application is None:
        raise LeaveNotFoundError("Leave application not found")
    if (
        application.manager_status != "approved"
        or application.hr_status != "pending"
        or application.status != "pending"
    ):
        raise LeaveConflictError("Leave application is not awaiting HR review")

    old_status = application.status
    application.hr_status = review.decision
    application.hr_id = hr_user.id
    application.hr_comment = review.comment
    application.hr_approved_at = datetime.now()
    application.status = review.decision

    action = (
        "HR_APPROVE_LEAVE"
        if review.decision == "approved"
        else "HR_REJECT_LEAVE"
    )
    create_audit_log(
        db=db,
        user_id=hr_user.id,
        action=action,
        resource_type="leave_application",
        resource_id=application.id,
        detail={
            "old_status": old_status,
            "new_status": application.status,
            "old_hr_status": "pending",
            "new_hr_status": review.decision,
            "comment": review.comment,
        },
        ip_address=ip_address,
    )
    notify_hr_reviewed(db, application, review.comment)
    return application


def review_leave_by_hr(
    db: Session,
    application_id: str,
    hr_user: User,
    review: LeaveReviewRequest,
    ip_address: str | None = None,
) -> LeaveApplication:
    application = review_leave_by_hr_mutation(
        db=db,
        application_id=application_id,
        hr_user=hr_user,
        review=review,
        ip_address=ip_address,
    )
    return _commit_and_refresh(db, application)


def review_leave_by_hr_idempotent(
    session_factory: SessionFactory,
    hr_user: User,
    application_id: str,
    review: LeaveReviewRequest,
    operation_id: str,
    confirmed_payload: dict[str, Any],
    ip_address: str | None = None,
) -> tuple[LeaveApplication, bool]:
    """Apply one actor-bound HR decision once and safely replay its result."""
    if hr_user.role != "hr":
        raise LeavePermissionError("Only HR can review leave applications")
    payload_data = dict(confirmed_payload)
    if (
        payload_data.get("application_id") != application_id
        or payload_data.get("decision") != review.decision
        or payload_data.get("comment") != review.comment
    ):
        raise IdempotencyConflictError(
            "Confirmed HR review does not match tool arguments"
        )
    actor_user_id = hr_user.id

    reservation_db = session_factory()
    try:
        operation, created = get_or_create_operation_record(
            reservation_db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=HR_REVIEW_OPERATION,
            payload=payload_data,
        )
        reservation_status = operation.status
        reservation_db.commit()
    except IdempotencyRecordError:
        _rollback_safely(reservation_db)
        raise
    except Exception as reservation_error:
        _rollback_safely(reservation_db)
        raise OperationResultUnknownError(
            "HR review reservation result could not be confirmed"
        ) from reservation_error
    finally:
        reservation_db.close()

    if not created:
        application = _return_or_reject_existing_operation(
            session_factory,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=HR_REVIEW_OPERATION,
            payload_data=payload_data,
        )
        if (
            application.hr_status != review.decision
            or application.status != review.decision
            or application.hr_id != actor_user_id
        ):
            raise OperationResultUnknownError(
                "HR review replay resource is inconsistent"
            )
        return application, True
    if reservation_status != "PROCESSING":
        raise IdempotencyConflictError("New HR review reservation is not PROCESSING")

    db = session_factory()
    try:
        operation = lock_operation_for_actor(
            db,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=HR_REVIEW_OPERATION,
            payload=payload_data,
        )
        if operation.status != "PROCESSING":
            raise IdempotencyConflictError(
                "Reserved HR review operation is not PROCESSING"
            )
        application = review_leave_by_hr_mutation(
            db=db,
            application_id=application_id,
            hr_user=hr_user,
            review=review,
            ip_address=ip_address,
        )
        db.flush()
        db.refresh(application)
        transition_processing_operation(
            operation,
            "SUCCEEDED",
            resource_type="leave_application",
            resource_id=application.id,
            result_payload={"application_id": application.id},
        )
        db.flush()
        db.expunge(application)
        _before_idempotent_commit()
    except (
        LeaveValidationError,
        LeaveConflictError,
        LeaveNotFoundError,
        LeavePermissionError,
    ) as error:
        if not _rollback_safely(db):
            db.close()
            raise OperationResultUnknownError(
                "HR review rollback could not be confirmed"
            ) from error
        db.close()
        error_code = {
            LeaveValidationError: "LEAVE_VALIDATION_ERROR",
            LeaveConflictError: "LEAVE_CONFLICT",
            LeaveNotFoundError: "LEAVE_NOT_FOUND",
            LeavePermissionError: "LEAVE_PERMISSION_DENIED",
        }[type(error)]
        try:
            _record_operation_terminal_state(
                session_factory,
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                operation_type=HR_REVIEW_OPERATION,
                payload_data=payload_data,
                status="FAILED",
                error_code=error_code,
            )
        except Exception as state_error:
            raise OperationResultUnknownError(
                "HR review failure state could not be persisted"
            ) from state_error
        raise
    except Exception as error:
        if not _rollback_safely(db):
            db.close()
            raise OperationResultUnknownError(
                "HR review rollback could not be confirmed"
            ) from error
        db.close()
        try:
            _record_operation_terminal_state(
                session_factory,
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                operation_type=HR_REVIEW_OPERATION,
                payload_data=payload_data,
                status="FAILED",
                error_code="EXECUTION_ABORTED",
            )
        except Exception as state_error:
            raise OperationResultUnknownError(
                "HR review failure state could not be persisted"
            ) from state_error
        raise

    try:
        _commit_idempotent_transaction(db)
    except Exception as commit_error:
        _rollback_safely(db)
        db.close()
        application = _resolve_uncertain_commit(
            session_factory,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            operation_type=HR_REVIEW_OPERATION,
            payload_data=payload_data,
            commit_error=commit_error,
        )
        if (
            application.hr_status != review.decision
            or application.status != review.decision
            or application.hr_id != actor_user_id
        ):
            raise OperationResultUnknownError(
                "HR review commit result is not safely replayable"
            )
        return application, True
    db.close()
    return application, False


def get_all_leave_applications(
    db: Session,
    employee_id: int | None = None,
    department: str | None = None,
    status: str | None = None,
    leave_type: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[LeaveApplication]:
    query = db.query(LeaveApplication)
    if department is not None:
        query = query.join(
            User,
            LeaveApplication.employee_id == User.id,
        ).filter(User.department == department)
    if employee_id is not None:
        query = query.filter(LeaveApplication.employee_id == employee_id)
    if status is not None:
        query = query.filter(LeaveApplication.status == status)
    if leave_type is not None:
        query = query.filter(LeaveApplication.leave_type == leave_type)
    query = _apply_date_filters(query, start_date, end_date)
    return (
        query.order_by(LeaveApplication.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
