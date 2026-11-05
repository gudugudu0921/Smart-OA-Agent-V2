"""Isolated SQLite coverage for manager-review idempotency semantics."""

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
from app.models.notification import Notification
from app.models.user import User
from app.schemas.leave import LeaveReviewRequest
from app.services.idempotency_service import (
    IdempotencyConflictError,
    OperationInProgressError,
    OperationNotFoundError,
    OperationResultUnknownError,
    get_or_create_operation_record,
    get_safe_operation_status,
    set_operation_status,
)
from app.services.leave_service import (
    LeaveConflictError,
    LeaveNotFoundError,
    LeavePermissionError,
    review_leave_by_manager_idempotent,
)


class ManagerReviewIdempotencyTests(unittest.TestCase):
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
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.manager = self.user(1, "manager")
        self.employee = self.user(2, "employee", manager_id=1)
        self.hr = self.user(3, "hr")
        self.other_manager = self.user(4, "manager")
        db = self.session_factory()
        db.add_all([self.manager, self.employee, self.hr, self.other_manager])
        db.commit()
        db.close()

    @staticmethod
    def user(user_id: int, role: str, manager_id: int | None = None) -> User:
        return User(
            id=user_id,
            username=f"manager_review_{user_id}",
            employee_no=f"MREVIEW{user_id:03d}",
            password_hash="not-used",
            name=f"Manager Review User {user_id}",
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
        manager_id: int = 1,
        status: str = "pending",
        manager_status: str = "pending",
    ) -> LeaveApplication:
        start = date.today() + timedelta(days=10)
        application = LeaveApplication(
            id=str(uuid4()),
            employee_id=self.employee.id,
            manager_id=manager_id,
            leave_type="annual",
            start_date=start,
            end_date=start + timedelta(days=1),
            leave_days=Decimal("2.0"),
            reason="manager review idempotency test",
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

    def payload(
        self,
        application: LeaveApplication,
        decision: str,
        comment: str | None,
    ) -> dict:
        return {
            "application_id": application.id,
            "employee_name": self.employee.name,
            "employee_no": self.employee.employee_no,
            "leave_type": application.leave_type,
            "start_date": application.start_date.isoformat(),
            "end_date": application.end_date.isoformat(),
            "status": "pending",
            "decision": decision,
            "comment": comment,
        }

    def execute(
        self,
        application: LeaveApplication,
        operation_id: str,
        *,
        decision: str = "approved",
        comment: str | None = "looks good",
        manager: User | None = None,
        payload: dict | None = None,
    ):
        return review_leave_by_manager_idempotent(
            self.session_factory,
            manager or self.manager,
            application.id,
            LeaveReviewRequest(decision=decision, comment=comment),
            operation_id,
            payload or self.payload(application, decision, comment),
        )

    def counts(self, application_id: str) -> tuple[int, int]:
        db = self.session_factory()
        result = (
            db.query(AuditLog)
            .filter(
                AuditLog.resource_id == application_id,
                AuditLog.action.in_(
                    {"MANAGER_APPROVE_LEAVE", "MANAGER_REJECT_LEAVE"}
                ),
            )
            .count(),
            db.query(Notification)
            .filter(Notification.related_application_id == application_id)
            .count(),
        )
        db.close()
        return result

    def test_approve_replays_without_duplicate_audit_or_notifications(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        first, first_replayed = self.execute(application, operation_id)
        replay, replayed = self.execute(application, operation_id)
        self.assertFalse(first_replayed)
        self.assertTrue(replayed)
        self.assertEqual(first.id, replay.id)
        self.assertEqual(first.status, "pending")
        self.assertEqual(first.manager_status, "approved")
        self.assertEqual(self.counts(application.id), (1, 2))
        db = self.session_factory()
        operation = db.get(BusinessOperation, operation_id)
        self.assertEqual(operation.status, "SUCCEEDED")
        safe = get_safe_operation_status(
            db,
            operation_id=operation_id,
            actor_user_id=self.manager.id,
        )
        self.assertEqual(safe["operation_type"], "MANAGER_REVIEW")
        self.assertEqual(safe["result_payload"], {"application_id": application.id})
        db.close()

    def test_reject_replays_one_terminal_rejection(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        first, _ = self.execute(
            application,
            operation_id,
            decision="rejected",
            comment="insufficient coverage",
        )
        replay, replayed = self.execute(
            application,
            operation_id,
            decision="rejected",
            comment="insufficient coverage",
        )
        self.assertTrue(replayed)
        self.assertEqual(first.id, replay.id)
        self.assertEqual(first.status, "rejected")
        self.assertEqual(first.manager_status, "rejected")
        self.assertEqual(self.counts(application.id), (1, 1))

    def test_assignment_role_and_processed_state_are_enforced(self) -> None:
        unassigned = self.application(manager_id=self.other_manager.id)
        unassigned_operation = str(uuid4())
        with self.assertRaises(LeaveNotFoundError):
            self.execute(unassigned, unassigned_operation)

        non_manager_application = self.application()
        with self.assertRaises(LeavePermissionError):
            self.execute(
                non_manager_application,
                str(uuid4()),
                manager=self.employee,
            )

        processed = self.application(status="rejected", manager_status="rejected")
        processed_operation = str(uuid4())
        with self.assertRaises(LeaveConflictError):
            self.execute(processed, processed_operation)
        db = self.session_factory()
        self.assertEqual(db.get(BusinessOperation, unassigned_operation).status, "FAILED")
        self.assertEqual(db.get(BusinessOperation, processed_operation).status, "FAILED")
        db.close()

    def test_same_operation_rejects_changed_target_decision_comment_or_actor(self) -> None:
        application = self.application()
        other = self.application()
        operation_id = str(uuid4())
        self.execute(application, operation_id)
        for changed in (
            {"application": other},
            {"decision": "rejected"},
            {"comment": "changed"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(IdempotencyConflictError):
                    self.execute(
                        changed.get("application", application),
                        operation_id,
                        decision=changed.get("decision", "approved"),
                        comment=changed.get("comment", "looks good"),
                    )
        with self.assertRaises(OperationNotFoundError):
            self.execute(application, operation_id, manager=self.other_manager)
        self.assertEqual(self.counts(application.id), (1, 2))

    def test_different_operations_cannot_review_one_application_twice(self) -> None:
        application = self.application()
        first_operation = str(uuid4())
        second_operation = str(uuid4())
        self.execute(application, first_operation)
        with self.assertRaises(LeaveConflictError):
            self.execute(application, second_operation)
        db = self.session_factory()
        self.assertEqual(db.get(BusinessOperation, first_operation).status, "SUCCEEDED")
        self.assertEqual(db.get(BusinessOperation, second_operation).status, "FAILED")
        db.close()
        self.assertEqual(self.counts(application.id), (1, 2))

    def test_processing_and_unknown_never_execute_review(self) -> None:
        for status, expected_error in (
            ("PROCESSING", OperationInProgressError),
            ("UNKNOWN", OperationResultUnknownError),
        ):
            with self.subTest(status=status):
                application = self.application()
                operation_id = str(uuid4())
                db = self.session_factory()
                operation, _ = get_or_create_operation_record(
                    db,
                    operation_id=operation_id,
                    actor_user_id=self.manager.id,
                    operation_type="MANAGER_REVIEW",
                    payload=self.payload(application, "approved", "looks good"),
                )
                if status == "UNKNOWN":
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
                saved = verify.get(LeaveApplication, application.id)
                self.assertEqual(saved.manager_status, "pending")
                verify.close()
                self.assertEqual(self.counts(application.id), (0, 0))

    def test_commit_uncertainty_never_fabricates_success(self) -> None:
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
        saved = db.get(LeaveApplication, application.id)
        self.assertEqual(saved.manager_status, "pending")
        db.close()
        self.assertEqual(self.counts(application.id), (0, 0))

    def test_lost_commit_acknowledgement_recovers_saved_result_once(self) -> None:
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
        self.assertTrue(replayed)
        self.assertEqual(result.manager_status, "approved")
        self.assertEqual(self.counts(application.id), (1, 2))


if __name__ == "__main__":
    unittest.main()
