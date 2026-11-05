"""Isolated SQLite coverage for cancellation idempotency semantics.

SQLite validates transaction/replay behavior here; MySQL row-lock concurrency remains
an explicit integration-test concern and is not claimed by this module.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import BigInteger, DefaultClause, Integer, MetaData, create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.db.mysql import Base
from app.models.audit_log import AuditLog
from app.models.business_operation import BusinessOperation
from app.models.leave_application import LeaveApplication
from app.models.user import User
from app.services.idempotency_service import (
    IdempotencyConflictError,
    OperationInProgressError,
    OperationNotFoundError,
    OperationResultUnknownError,
    get_safe_operation_status,
    get_or_create_operation_record,
    set_operation_status,
)
from app.services.leave_service import (
    LeaveConflictError,
    LeaveNotFoundError,
    cancel_leave_application_idempotent,
)


class CancelLeaveIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

        metadata = MetaData()
        for table in Base.metadata.sorted_tables:
            clone = table.to_metadata(metadata)
            for column in clone.columns:
                if isinstance(column.type, BigInteger):
                    column.type = Integer()
                if (
                    column.server_default is not None
                    and "ON UPDATE" in str(column.server_default.arg)
                ):
                    column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
        metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )
        db = self.session_factory()
        self.manager = self.user(1, "manager")
        self.employee = self.user(2, "employee", manager_id=1)
        self.other_employee = self.user(3, "employee", manager_id=1)
        db.add_all([self.manager, self.employee, self.other_employee])
        db.commit()
        db.close()

    @staticmethod
    def user(user_id: int, role: str, manager_id: int | None = None) -> User:
        return User(
            id=user_id,
            username=f"cancel_{user_id}",
            employee_no=f"CANCEL{user_id:03d}",
            password_hash="not-used",
            name=f"Cancel User {user_id}",
            role=role,
            department="Testing",
            manager_id=manager_id,
            is_active=True,
            must_change_password=False,
        )

    def tearDown(self) -> None:
        self.engine.dispose()

    def application(
        self,
        *,
        employee_id: int = 2,
        status: str = "pending",
        manager_status: str = "pending",
    ) -> LeaveApplication:
        start = date.today() + timedelta(days=10)
        application = LeaveApplication(
            id=str(uuid4()),
            employee_id=employee_id,
            manager_id=1,
            leave_type="annual",
            start_date=start,
            end_date=start + timedelta(days=1),
            leave_days=Decimal("2.0"),
            reason="cancel idempotency test",
            status=status,
            manager_status=manager_status,
            hr_status="pending",
        )
        db = self.session_factory()
        db.add(application)
        db.commit()
        db.expunge(application)
        db.close()
        return application

    @staticmethod
    def payload(application: LeaveApplication) -> dict:
        return {
            "application_id": application.id,
            "leave_type": application.leave_type,
            "start_date": application.start_date.isoformat(),
            "end_date": application.end_date.isoformat(),
            "status": "pending",
        }

    def execute(
        self,
        application: LeaveApplication,
        operation_id: str,
        *,
        employee: User | None = None,
        payload: dict | None = None,
    ):
        return cancel_leave_application_idempotent(
            self.session_factory,
            employee or self.employee,
            application.id,
            operation_id,
            payload or self.payload(application),
        )

    def test_success_replays_without_second_cancellation_or_audit(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        first, first_replayed = self.execute(application, operation_id)
        second, second_replayed = self.execute(application, operation_id)
        self.assertEqual(first.status, "cancelled")
        self.assertEqual(second.id, application.id)
        self.assertFalse(first_replayed)
        self.assertTrue(second_replayed)

        db = self.session_factory()
        self.assertEqual(
            db.query(AuditLog).filter(AuditLog.action == "CANCEL_LEAVE").count(),
            1,
        )
        operation = db.get(BusinessOperation, operation_id)
        self.assertEqual(operation.status, "SUCCEEDED")
        self.assertEqual(operation.resource_id, application.id)
        safe_status = get_safe_operation_status(
            db,
            operation_id=operation_id,
            actor_user_id=self.employee.id,
        )
        self.assertEqual(safe_status["operation_type"], "CANCEL_LEAVE")
        self.assertEqual(
            safe_status["result_payload"],
            {"application_id": application.id},
        )
        db.close()

    def test_same_operation_with_different_target_or_actor_is_rejected(self) -> None:
        first = self.application()
        second = self.application()
        operation_id = str(uuid4())
        self.execute(first, operation_id)
        with self.assertRaises(IdempotencyConflictError):
            self.execute(second, operation_id)
        with self.assertRaises(OperationNotFoundError):
            self.execute(first, operation_id, employee=self.other_employee)

    def test_different_operation_ids_cannot_cancel_the_same_application_twice(self) -> None:
        application = self.application()
        first_operation = str(uuid4())
        second_operation = str(uuid4())
        self.execute(application, first_operation)
        with self.assertRaises(LeaveConflictError):
            self.execute(application, second_operation)
        db = self.session_factory()
        self.assertEqual(
            db.query(AuditLog).filter(AuditLog.action == "CANCEL_LEAVE").count(),
            1,
        )
        self.assertEqual(db.get(BusinessOperation, first_operation).status, "SUCCEEDED")
        self.assertEqual(db.get(BusinessOperation, second_operation).status, "FAILED")
        db.close()

    def test_other_owners_application_is_rejected_and_recorded_failed(self) -> None:
        application = self.application(employee_id=self.other_employee.id)
        operation_id = str(uuid4())
        with self.assertRaises(LeaveNotFoundError):
            self.execute(application, operation_id)
        db = self.session_factory()
        self.assertEqual(db.get(BusinessOperation, operation_id).status, "FAILED")
        self.assertEqual(db.get(LeaveApplication, application.id).status, "pending")
        db.close()

    def test_non_cancellable_state_is_rejected_without_mutation(self) -> None:
        application = self.application(status="approved", manager_status="approved")
        operation_id = str(uuid4())
        with self.assertRaises(LeaveConflictError):
            self.execute(application, operation_id)
        db = self.session_factory()
        self.assertEqual(db.get(BusinessOperation, operation_id).status, "FAILED")
        self.assertEqual(db.get(LeaveApplication, application.id).status, "approved")
        db.close()

    def test_processing_and_unknown_records_are_never_reexecuted(self) -> None:
        for terminal_status, expected_error in (
            ("PROCESSING", OperationInProgressError),
            ("UNKNOWN", OperationResultUnknownError),
        ):
            with self.subTest(status=terminal_status):
                application = self.application()
                operation_id = str(uuid4())
                db = self.session_factory()
                operation, _ = get_or_create_operation_record(
                    db,
                    operation_id=operation_id,
                    actor_user_id=self.employee.id,
                    operation_type="CANCEL_LEAVE",
                    payload=self.payload(application),
                )
                if terminal_status == "UNKNOWN":
                    set_operation_status(
                        operation,
                        "UNKNOWN",
                        error_code="COMMIT_RESULT_UNKNOWN",
                    )
                db.commit()
                db.close()
                with self.assertRaises(expected_error):
                    self.execute(application, operation_id)
                verify = self.session_factory()
                self.assertEqual(
                    verify.get(LeaveApplication, application.id).status,
                    "pending",
                )
                verify.close()

    def test_commit_error_without_committed_business_result_becomes_unknown(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        with patch(
            "app.services.leave_service._commit_idempotent_transaction",
            side_effect=RuntimeError("simulated commit uncertainty"),
        ):
            with self.assertRaises(OperationResultUnknownError):
                self.execute(application, operation_id)
        db = self.session_factory()
        self.assertEqual(db.get(BusinessOperation, operation_id).status, "UNKNOWN")
        self.assertEqual(db.get(LeaveApplication, application.id).status, "pending")
        db.close()

    def test_post_commit_error_replays_the_committed_success(self) -> None:
        application = self.application()
        operation_id = str(uuid4())

        def commit_then_raise(db):
            db.commit()
            raise RuntimeError("simulated lost commit acknowledgement")

        with patch(
            "app.services.leave_service._commit_idempotent_transaction",
            side_effect=commit_then_raise,
        ):
            result, replayed = self.execute(application, operation_id)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(replayed)
        db = self.session_factory()
        self.assertEqual(db.get(BusinessOperation, operation_id).status, "SUCCEEDED")
        self.assertEqual(db.get(LeaveApplication, application.id).status, "cancelled")
        self.assertEqual(
            db.query(AuditLog).filter(AuditLog.action == "CANCEL_LEAVE").count(),
            1,
        )
        db.close()


if __name__ == "__main__":
    unittest.main()
