from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.business_operation import BusinessOperation
from app.models.leave_application import LeaveApplication
from app.models.notification import Notification
from app.models.user import User
from app.schemas.leave import LeaveCreate, LeaveReviewRequest
from app.services import leave_service
from app.services.idempotency_service import (
    IdempotencyConflictError,
    OperationInProgressError,
    OperationNotFoundError,
    OperationResultUnknownError,
    get_or_create_operation_record,
    transition_processing_operation,
)


TEST_DATABASE_ENV = "SMART_OA_CREATE_LEAVE_TEST_DATABASE_URL"
TEST_DATABASE_PREFIX = "smartoa_phase2a2_"


def execute_operation_migration(engine) -> None:
    migration_path = (
        Path(__file__).parents[2]
        / "app"
        / "db"
        / "migrations"
        / "phase2_business_operations.sql"
    )
    sql = "\n".join(
        line
        for line in migration_path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    ).strip()
    statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


def make_user(
    user_id: int,
    *,
    role: str,
    manager_id: int | None = None,
) -> User:
    return User(
        id=user_id,
        employee_no=f"MYSQLCREATE{user_id}",
        username=f"mysql_create_{user_id}",
        password_hash="unused-in-isolated-mysql-tests",
        name=f"MySQL Create Leave User {user_id}",
        role=role,
        department="Testing",
        manager_id=manager_id,
        is_active=True,
        must_change_password=False,
    )


class MySQLCreateLeaveIdempotencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.getenv(TEST_DATABASE_ENV)
        if not database_url:
            raise unittest.SkipTest(
                f"{TEST_DATABASE_ENV} is required for isolated MySQL integration tests"
            )
        parsed = make_url(database_url)
        if parsed.drivername != "mysql+pymysql":
            raise RuntimeError("Integration URL must use mysql+pymysql")
        if parsed.host not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("Integration MySQL must bind to loopback")
        if not (parsed.database or "").startswith(TEST_DATABASE_PREFIX):
            raise RuntimeError("Refusing to use a non-Phase-2A-2 database")

        cls.engine = create_engine(database_url, pool_pre_ping=True, pool_timeout=3)
        User.__table__.create(cls.engine, checkfirst=True)
        LeaveApplication.__table__.create(cls.engine, checkfirst=True)
        Notification.__table__.create(cls.engine, checkfirst=True)
        AuditLog.__table__.create(cls.engine, checkfirst=True)
        execute_operation_migration(cls.engine)
        cls.SessionFactory = sessionmaker(
            bind=cls.engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        with cls.SessionFactory() as db:
            fixtures = (
                make_user(9301, role="manager"),
                make_user(9302, role="employee", manager_id=9301),
                make_user(9303, role="employee", manager_id=9301),
                make_user(9304, role="hr"),
            )
            for fixture in fixtures:
                if db.get(User, fixture.id) is None:
                    db.add(fixture)
            db.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM notifications"))
            connection.execute(text("DELETE FROM audit_logs"))
            connection.execute(text("DELETE FROM leave_applications"))
            connection.execute(text("DELETE FROM business_operations"))
        self.payload = LeaveCreate(
            leave_type="annual",
            start_date=date(2032, 3, 1),
            end_date=date(2032, 3, 2),
            reason="isolated MySQL idempotency test",
        )

    def user(self, user_id: int) -> User:
        with self.SessionFactory() as db:
            user = db.get(User, user_id)
            self.assertIsNotNone(user)
            db.expunge(user)
            return user

    def create(
        self,
        operation_id: str,
        *,
        employee_id: int = 9302,
        payload: LeaveCreate | None = None,
    ) -> LeaveApplication:
        return leave_service.create_leave_application_idempotent(
            self.SessionFactory,
            self.user(employee_id),
            payload or self.payload,
            operation_id,
            ip_address="127.0.0.1",
        )

    def reserve(
        self,
        operation_id: str,
        *,
        operation_type: str = "SUBMIT_LEAVE",
        employee_id: int = 9302,
        payload: LeaveCreate | None = None,
    ) -> BusinessOperation:
        payload_data = (payload or self.payload).model_dump(mode="python")
        with self.SessionFactory() as db:
            operation, _created = get_or_create_operation_record(
                db,
                operation_id=operation_id,
                actor_user_id=employee_id,
                operation_type=operation_type,
                payload=payload_data,
            )
            db.commit()
            return operation

    def counts(self) -> tuple[int, int, int]:
        with self.SessionFactory() as db:
            return (
                db.query(LeaveApplication).count(),
                db.query(AuditLog).filter(AuditLog.action == "CREATE_LEAVE").count(),
                db.query(Notification).filter(Notification.type == "leave_created").count(),
            )

    def test_first_create_and_same_payload_replay_return_one_result(self) -> None:
        operation_id = str(uuid4())
        first = self.create(operation_id)
        replay = self.create(operation_id)
        self.assertEqual(replay.id, first.id)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, operation_id)
            self.assertEqual(operation.status, "SUCCEEDED")
            self.assertEqual(operation.resource_id, first.id)
            self.assertEqual(operation.result_payload, {"application_id": first.id})
            with self.assertRaises(IdempotencyConflictError):
                transition_processing_operation(
                    operation,
                    "FAILED",
                    error_code="MUST_NOT_OVERWRITE_SUCCESS",
                )

    def test_payload_type_and_actor_conflicts_do_not_replay(self) -> None:
        operation_id = str(uuid4())
        self.create(operation_id)
        changed = self.payload.model_copy(update={"reason": "changed"})
        with self.assertRaises(IdempotencyConflictError):
            self.create(operation_id, payload=changed)
        with self.assertRaises(OperationNotFoundError):
            self.create(operation_id, employee_id=9303)

        different_type_id = str(uuid4())
        self.reserve(different_type_id, operation_type="CANCEL_LEAVE")
        with self.assertRaises(IdempotencyConflictError):
            self.create(different_type_id)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_same_dates_with_different_key_use_existing_overlap_rule(self) -> None:
        self.create(str(uuid4()))
        second_id = str(uuid4())
        with self.assertRaises(leave_service.LeaveConflictError):
            self.create(second_id)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, second_id)
            self.assertEqual(operation.status, "FAILED")
            self.assertEqual(operation.error_code, "LEAVE_CONFLICT")

    def test_active_processing_and_unknown_never_execute(self) -> None:
        processing_id = str(uuid4())
        self.reserve(processing_id)
        with self.assertRaises(OperationInProgressError):
            self.create(processing_id)

        unknown_id = str(uuid4())
        self.reserve(unknown_id)
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, unknown_id)
            transition_processing_operation(
                operation,
                "UNKNOWN",
                error_code="TEST_UNKNOWN",
            )
            db.commit()
        with self.assertRaises(OperationResultUnknownError):
            self.create(unknown_id)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_concurrent_same_key_creates_exactly_one_business_result(self) -> None:
        operation_id = str(uuid4())
        worker_count = 6
        barrier = Barrier(worker_count)

        def worker(_index: int):
            barrier.wait(timeout=10)
            try:
                return ("ok", self.create(operation_id).id)
            except OperationInProgressError:
                return ("processing", None)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(worker, range(worker_count)))
        successful_ids = {result[1] for result in results if result[0] == "ok"}
        self.assertEqual(len(successful_ids), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_employee_row_lock_serializes_different_keys_for_same_dates(self) -> None:
        operation_ids = (str(uuid4()), str(uuid4()))
        barrier = Barrier(2)

        def worker(operation_id: str) -> str:
            barrier.wait(timeout=10)
            try:
                self.create(operation_id)
                return "created"
            except leave_service.LeaveConflictError:
                return "overlap"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(worker, operation_ids))
        self.assertEqual(sorted(outcomes), ["created", "overlap"])
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.SessionFactory() as db:
            statuses = {
                db.get(BusinessOperation, operation_id).status
                for operation_id in operation_ids
            }
            self.assertEqual(statuses, {"SUCCEEDED", "FAILED"})

    def test_business_exception_rolls_back_leave_audit_and_notification(self) -> None:
        operation_id = str(uuid4())
        with (
            patch(
                "app.services.leave_service.notify_leave_created",
                side_effect=RuntimeError("injected before commit"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected before commit"),
        ):
            self.create(operation_id)
        self.assertEqual(self.counts(), (0, 0, 0))
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, operation_id)
            self.assertEqual(operation.status, "FAILED")
            self.assertNotEqual(operation.status, "SUCCEEDED")

    def test_failure_between_flush_and_commit_is_atomic_and_failed(self) -> None:
        operation_id = str(uuid4())
        with (
            patch(
                "app.services.leave_service._before_idempotent_commit",
                side_effect=ConnectionError("injected before commit"),
            ),
            self.assertRaisesRegex(ConnectionError, "injected before commit"),
        ):
            self.create(operation_id)
        self.assertEqual(self.counts(), (0, 0, 0))
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, operation_id)
            self.assertEqual(operation.status, "FAILED")
            self.assertEqual(operation.error_code, "EXECUTION_ABORTED")

    def test_commit_error_and_verification_failure_never_retries_the_write(self) -> None:
        operation_id = str(uuid4())
        real_lock = leave_service.lock_operation_for_actor
        lock_calls = 0

        def fail_only_verification(*args, **kwargs):
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 2:
                raise ConnectionError("injected verification outage")
            return real_lock(*args, **kwargs)

        with (
            patch(
                "app.services.leave_service.lock_operation_for_actor",
                side_effect=fail_only_verification,
            ),
            patch(
                "app.services.leave_service._commit_idempotent_transaction",
                side_effect=ConnectionError("injected commit error"),
            ),
            self.assertRaises(OperationResultUnknownError),
        ):
            self.create(operation_id)
        self.assertEqual(lock_calls, 2)
        self.assertEqual(self.counts(), (0, 0, 0))
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, operation_id)
            self.assertEqual(operation.status, "PROCESSING")

    def test_commit_success_with_lost_response_recovers_the_saved_result(self) -> None:
        operation_id = str(uuid4())
        real_commit = leave_service._commit_idempotent_transaction

        def commit_then_lose_response(db) -> None:
            real_commit(db)
            raise ConnectionError("injected response loss")

        with patch(
            "app.services.leave_service._commit_idempotent_transaction",
            side_effect=commit_then_lose_response,
        ):
            recovered = self.create(operation_id)
        replay = self.create(operation_id)
        self.assertEqual(replay.id, recovered.id)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_legacy_wrapper_still_commits_without_operation_id(self) -> None:
        with self.SessionFactory() as db:
            application = leave_service.create_leave_application(
                db,
                self.user(9302),
                self.payload,
                ip_address="127.0.0.1",
            )
            self.assertIsNotNone(application.created_at)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.SessionFactory() as db:
            self.assertEqual(db.query(BusinessOperation).count(), 0)

    def test_mutation_function_never_commits_its_session(self) -> None:
        with self.SessionFactory() as db:
            with patch.object(db, "commit", wraps=db.commit) as commit:
                leave_service.create_leave_application_mutation(
                    db,
                    self.user(9302),
                    self.payload,
                    ip_address="127.0.0.1",
                )
                db.flush()
                commit.assert_not_called()
            db.rollback()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_reservation_and_business_transactions_use_distinct_sessions(self) -> None:
        sessions = []

        def tracking_factory():
            db = self.SessionFactory()
            sessions.append(db)
            return db

        application = leave_service.create_leave_application_idempotent(
            tracking_factory,
            self.user(9302),
            self.payload,
            str(uuid4()),
        )
        self.assertIsNotNone(application.id)
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])

    def test_non_target_cancel_manager_and_hr_workflows_remain_unchanged(self) -> None:
        employee = self.user(9302)
        manager = self.user(9301)
        hr_user = self.user(9304)
        with self.SessionFactory() as db:
            cancelled = leave_service.create_leave_application(db, employee, self.payload)
            leave_service.cancel_leave_application(db, cancelled.id, employee)
            approved_payload = self.payload.model_copy(
                update={
                    "start_date": date(2032, 4, 1),
                    "end_date": date(2032, 4, 2),
                }
            )
            approved = leave_service.create_leave_application(
                db,
                employee,
                approved_payload,
            )
            leave_service.review_leave_by_manager(
                db,
                approved.id,
                manager,
                LeaveReviewRequest(decision="approved"),
            )
            leave_service.review_leave_by_hr(
                db,
                approved.id,
                hr_user,
                LeaveReviewRequest(decision="approved"),
            )
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(approved.status, "approved")

    def test_non_employee_is_rejected_before_reservation(self) -> None:
        with self.assertRaises(leave_service.LeavePermissionError):
            self.create(str(uuid4()), employee_id=9301)
        with self.SessionFactory() as db:
            self.assertEqual(db.query(BusinessOperation).count(), 0)


if __name__ == "__main__":
    unittest.main()
