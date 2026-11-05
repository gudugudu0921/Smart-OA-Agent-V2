"""Isolated SQLite coverage for HR-review idempotency semantics."""

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
    OperationPreviouslyFailedError,
    OperationResultUnknownError,
    get_or_create_operation_record,
    get_safe_operation_status,
    set_operation_status,
)
from app.services.leave_service import (
    LeaveConflictError,
    LeavePermissionError,
    review_leave_by_hr_idempotent,
)


class HrReviewIdempotencyTests(unittest.TestCase):
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
                if column.server_default is not None and "ON UPDATE" in str(column.server_default.arg):
                    column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
        metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.manager = self.user(1, "manager")
        self.employee = self.user(2, "employee", manager_id=1)
        self.hr = self.user(3, "hr")
        self.other_hr = self.user(4, "hr")
        db = self.session_factory()
        db.add_all([self.manager, self.employee, self.hr, self.other_hr])
        db.commit()
        db.close()

    @staticmethod
    def user(user_id: int, role: str, manager_id: int | None = None) -> User:
        return User(
            id=user_id,
            username=f"hr_review_{user_id}",
            employee_no=f"HREVIEW{user_id:03d}",
            password_hash="not-used",
            name=f"HR Review User {user_id}",
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
        status: str = "pending",
        manager_status: str = "approved",
        hr_status: str = "pending",
    ) -> LeaveApplication:
        start = date.today() + timedelta(days=10)
        application = LeaveApplication(
            id=str(uuid4()),
            employee_id=self.employee.id,
            manager_id=self.manager.id,
            leave_type="annual",
            start_date=start,
            end_date=start + timedelta(days=1),
            leave_days=Decimal("2.0"),
            reason="HR review idempotency test",
            status=status,
            manager_status=manager_status,
            hr_status=hr_status,
        )
        db = self.session_factory()
        db.add(application)
        db.commit()
        db.expunge(application)
        db.close()
        return application

    def payload(self, application, decision="approved", comment="final review") -> dict:
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
        application,
        operation_id,
        *,
        decision="approved",
        comment="final review",
        actor=None,
        payload=None,
    ):
        return review_leave_by_hr_idempotent(
            self.session_factory,
            actor or self.hr,
            application.id,
            LeaveReviewRequest(decision=decision, comment=comment),
            operation_id,
            payload or self.payload(application, decision, comment),
        )

    def counts(self, application_id: str) -> tuple[int, int]:
        db = self.session_factory()
        result = (
            db.query(AuditLog).filter(
                AuditLog.resource_id == application_id,
                AuditLog.action.in_({"HR_APPROVE_LEAVE", "HR_REJECT_LEAVE"}),
            ).count(),
            db.query(Notification).filter(
                Notification.related_application_id == application_id
            ).count(),
        )
        db.close()
        return result

    def test_approve_and_reject_replay_once_to_final_state(self) -> None:
        for decision in ("approved", "rejected"):
            with self.subTest(decision=decision):
                application = self.application()
                operation_id = str(uuid4())
                first, first_replayed = self.execute(
                    application, operation_id, decision=decision
                )
                replay, replayed = self.execute(
                    application, operation_id, decision=decision
                )
                self.assertFalse(first_replayed)
                self.assertTrue(replayed)
                self.assertEqual(first.id, replay.id)
                self.assertEqual(first.status, decision)
                self.assertEqual(first.hr_status, decision)
                self.assertEqual(first.hr_id, self.hr.id)
                self.assertEqual(self.counts(application.id), (1, 1))
                db = self.session_factory()
                safe = get_safe_operation_status(
                    db, operation_id=operation_id, actor_user_id=self.hr.id
                )
                self.assertEqual(safe["operation_type"], "HR_REVIEW")
                db.close()

    def test_role_and_reviewable_state_are_enforced(self) -> None:
        with self.assertRaises(LeavePermissionError):
            self.execute(self.application(), str(uuid4()), actor=self.manager)
        for application in (
            self.application(manager_status="pending"),
            self.application(status="approved", hr_status="approved"),
        ):
            operation_id = str(uuid4())
            with self.assertRaises(LeaveConflictError):
                self.execute(application, operation_id)
            db = self.session_factory()
            self.assertEqual(db.get(BusinessOperation, operation_id).status, "FAILED")
            db.close()

    def test_same_operation_rejects_changed_actor_target_decision_or_comment(self) -> None:
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
                        comment=changed.get("comment", "final review"),
                    )
        with self.assertRaises(OperationNotFoundError):
            self.execute(application, operation_id, actor=self.other_hr)
        self.assertEqual(self.counts(application.id), (1, 1))

    def test_different_operations_cannot_final_review_twice(self) -> None:
        application = self.application()
        first_operation = str(uuid4())
        second_operation = str(uuid4())
        self.execute(application, first_operation)
        with self.assertRaises(LeaveConflictError):
            self.execute(application, second_operation)
        self.assertEqual(self.counts(application.id), (1, 1))

    def test_processing_failed_and_unknown_never_execute_review(self) -> None:
        for status, expected_error in (
            ("PROCESSING", OperationInProgressError),
            ("FAILED", OperationPreviouslyFailedError),
            ("UNKNOWN", OperationResultUnknownError),
        ):
            with self.subTest(status=status):
                application = self.application()
                operation_id = str(uuid4())
                db = self.session_factory()
                operation, _ = get_or_create_operation_record(
                    db,
                    operation_id=operation_id,
                    actor_user_id=self.hr.id,
                    operation_type="HR_REVIEW",
                    payload=self.payload(application),
                )
                if status != "PROCESSING":
                    set_operation_status(
                        operation,
                        status,
                        error_code=(
                            "EXECUTION_ABORTED"
                            if status == "FAILED"
                            else "COMMIT_RESULT_UNKNOWN"
                        ),
                    )
                db.commit()
                db.close()
                with self.assertRaises(expected_error):
                    self.execute(application, operation_id)
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
        self.assertEqual(db.get(LeaveApplication, application.id).hr_status, "pending")
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
        self.assertEqual(result.status, "approved")
        self.assertEqual(self.counts(application.id), (1, 1))


if __name__ == "__main__":
    unittest.main()
