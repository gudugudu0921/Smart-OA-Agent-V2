"""Exercise real services and persistence using an isolated SQLite database.

Only test DDL is adapted for SQLite; application models and services are unchanged.
MySQL-specific locking and concurrent submissions require integration testing.
"""

from datetime import date, timedelta
from decimal import Decimal
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.runnables import RunnableLambda
from sqlalchemy import BigInteger, DefaultClause, Integer, MetaData, create_engine, event, text
from sqlalchemy.orm import Session

from app.agents.chat_agent import run_chat_agent
from app.agents.recognition import IntentResult
from app.db.mysql import Base
from app.models.audit_log import AuditLog
from app.models.leave_application import LeaveApplication
from app.models.notification import Notification
from app.models.user import User
from app.schemas.leave import LeaveCreate, LeaveReviewRequest
from app.services import leave_service as service


class LeaveWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _):
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
        self.db = Session(self.engine, expire_on_commit=False)
        self.manager = self.user(1, "manager")
        self.other_manager = self.user(2, "manager")
        self.hr = self.user(3, "hr")
        self.employee = self.user(4, "employee", manager_id=1)
        self.other_employee = self.user(5, "employee", manager_id=2)
        self.db.add_all([self.manager, self.other_manager, self.hr])
        self.db.commit()
        self.db.add_all([self.employee, self.other_employee])
        self.db.commit()
        self.start = date.today() + timedelta(days=30)
        self.payload = LeaveCreate(
            leave_type="annual", start_date=self.start,
            end_date=self.start + timedelta(days=1), reason="Fictional demo leave",
        )

    @staticmethod
    def user(user_id, role, manager_id=None):
        return User(
            id=user_id, username=f"demo_{user_id}", employee_no=f"DEMO{user_id:03d}",
            password_hash="unused-in-service-tests", name=f"Demo User {user_id}",
            role=role, department="Demo", manager_id=manager_id,
            is_active=True, must_change_password=False,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def submit(self):
        return service.create_leave_application(self.db, self.employee, self.payload)

    def review(self, decision="approved"):
        return LeaveReviewRequest(decision=decision, comment="Fictional review")

    def test_full_approval_persists_status_notifications_and_audit(self):
        application = self.submit()
        self.assertEqual(application.leave_days, Decimal("2.0"))
        self.assertEqual([x.id for x in service.get_manager_pending_applications(self.db, 1)], [application.id])
        self.assertEqual(service.get_hr_pending_applications(self.db), [])
        service.review_leave_by_manager(self.db, application.id, self.manager, self.review())
        self.assertEqual(application.status, "pending")
        self.assertEqual([x.id for x in service.get_hr_pending_applications(self.db)], [application.id])
        service.review_leave_by_hr(self.db, application.id, self.hr, self.review())
        self.db.expire_all()
        saved = self.db.get(LeaveApplication, application.id)
        self.assertEqual((saved.status, saved.manager_status, saved.hr_status), ("approved",) * 3)
        self.assertEqual(saved.hr_id, self.hr.id)
        self.assertEqual(service.get_hr_pending_applications(self.db), [])
        self.assertEqual(
            [x.action for x in self.db.query(AuditLog).order_by(AuditLog.id)],
            ["CREATE_LEAVE", "MANAGER_APPROVE_LEAVE", "HR_APPROVE_LEAVE"],
        )
        self.assertEqual(
            {(x.user_id, x.type) for x in self.db.query(Notification)},
            {(1, "leave_created"), (4, "leave_manager_approved"),
             (3, "leave_hr_pending"), (4, "leave_hr_approved")},
        )

    def test_hr_cannot_skip_manager_review(self):
        application = self.submit()
        with self.assertRaises(service.LeaveConflictError):
            service.review_leave_by_hr(self.db, application.id, self.hr, self.review())
        self.assertEqual(application.hr_status, "pending")
        self.assertEqual(self.db.query(AuditLog).count(), 1)

    def test_unrelated_manager_and_employee_cannot_access_application(self):
        application = self.submit()
        with self.assertRaises(service.LeaveNotFoundError):
            service.review_leave_by_manager(self.db, application.id, self.other_manager, self.review())
        with self.assertRaises(service.LeaveNotFoundError):
            service.get_leave_application(self.db, application.id, self.other_employee)
        with self.assertRaises(service.LeaveNotFoundError):
            service.cancel_leave_application(self.db, application.id, self.other_employee)
        self.assertEqual(application.status, "pending")

    def test_manager_rejection_is_final(self):
        application = self.submit()
        service.review_leave_by_manager(self.db, application.id, self.manager, self.review("rejected"))
        self.assertEqual(application.status, "rejected")
        self.assertEqual(service.get_hr_pending_applications(self.db), [])
        with self.assertRaises(service.LeaveConflictError):
            service.review_leave_by_hr(self.db, application.id, self.hr, self.review())
        self.assertEqual(self.db.query(Notification).filter_by(type="leave_manager_rejected").count(), 1)

    def test_hr_rejection_is_final_and_cannot_be_reviewed_again(self):
        application = self.submit()
        service.review_leave_by_manager(self.db, application.id, self.manager, self.review())
        service.review_leave_by_hr(self.db, application.id, self.hr, self.review("rejected"))
        with self.assertRaises(service.LeaveConflictError):
            service.review_leave_by_hr(self.db, application.id, self.hr, self.review())
        self.assertEqual(application.status, "rejected")
        self.assertEqual(self.db.query(Notification).filter_by(type="leave_hr_rejected").count(), 1)

    def test_cancelled_application_cannot_be_approved_and_dates_can_be_reused(self):
        application = self.submit()
        service.cancel_leave_application(self.db, application.id, self.employee)
        with self.assertRaises(service.LeaveConflictError):
            service.review_leave_by_manager(self.db, application.id, self.manager, self.review())
        replacement = self.submit()
        self.assertNotEqual(replacement.id, application.id)
        self.assertEqual(self.db.query(LeaveApplication).count(), 2)

    def test_manager_approved_application_cannot_be_cancelled_or_reviewed_again(self):
        application = self.submit()
        service.review_leave_by_manager(self.db, application.id, self.manager, self.review())
        with self.assertRaises(service.LeaveConflictError):
            service.cancel_leave_application(self.db, application.id, self.employee)
        with self.assertRaises(service.LeaveConflictError):
            service.review_leave_by_manager(self.db, application.id, self.manager, self.review())

    def test_overlapping_pending_and_approved_dates_are_rejected(self):
        application = self.submit()
        with self.assertRaises(service.LeaveConflictError):
            self.submit()
        service.review_leave_by_manager(self.db, application.id, self.manager, self.review())
        service.review_leave_by_hr(self.db, application.id, self.hr, self.review())
        with self.assertRaises(service.LeaveConflictError):
            self.submit()
        self.assertEqual(self.db.query(LeaveApplication).count(), 1)

    def test_agent_collects_fields_then_waits_for_confirmation_without_writing(self):
        llm = MagicMock()
        results = iter([
            IntentResult(intent="submit_leave", leave_type="annual"),
            IntentResult(intent="submit_leave", start_date=self.start,
                         end_date=self.start, is_followup=True),
        ])
        llm.with_structured_output.return_value = RunnableLambda(lambda _: next(results))
        with (
            patch("app.agents.chat_agent.get_redis_checkpointer", return_value=InMemorySaver()),
            patch("app.agents.recognition.get_llm", return_value=llm),
        ):
            operation_id = str(uuid4())
            first = run_chat_agent(
                self.db,
                self.employee,
                "demo-thread",
                "我要请年假",
                message_request_id=str(uuid4()),
                operation_id=operation_id,
                task_revision=0,
            )
            self.assertEqual(first["error"], "missing_fields")
            self.assertEqual(self.db.query(LeaveApplication).count(), 0)
            second = run_chat_agent(
                self.db,
                self.employee,
                "demo-thread",
                f"{self.start.isoformat()}一天",
                message_request_id=str(uuid4()),
                operation_id=operation_id,
                task_revision=first["task_revision"],
            )
        self.assertFalse(second["success"])
        self.assertEqual(second["task_phase"], "AWAITING_CONFIRMATION")
        self.assertEqual(second["code"], "CHAT_CONFIRMATION_REQUIRED")
        self.assertEqual(self.db.query(LeaveApplication).count(), 0)
        self.assertEqual(self.db.query(AuditLog).count(), 0)

    def test_agent_blocks_employee_from_manager_approval(self):
        application = self.submit()
        with patch("app.agents.chat_agent.get_redis_checkpointer", return_value=InMemorySaver()):
            result = run_chat_agent(
                self.db, self.employee, "unauthorized-thread", "approve",
                recognizer=lambda _: {"intent": "manager_review", "error": None,
                                      "extracted_info": {"application_id": application.id, "decision": "approved"}},
            )
        self.assertEqual(result["error"], "permission_denied")
        self.assertEqual(application.manager_status, "pending")
        self.assertEqual(self.db.query(AuditLog).count(), 1)
