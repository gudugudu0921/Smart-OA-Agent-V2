from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.business_operation import BusinessOperation
from app.models.leave_application import LeaveApplication
from app.models.notification import Notification
from app.models.user import User
from app.schemas.leave import LeaveReviewRequest
from app.services import leave_service
from app.services.idempotency_service import (
    OperationInProgressError,
    OperationResultUnknownError,
    get_or_create_operation_record,
    transition_processing_operation,
)


TEST_DATABASE_ENV = "SMART_OA_MANAGER_REVIEW_TEST_DATABASE_URL"
TEST_SERVER_UUID_ENV = "SMART_OA_MANAGER_REVIEW_TEST_SERVER_UUID"
TEST_CONTAINER_HOSTNAME_ENV = "SMART_OA_MANAGER_REVIEW_TEST_CONTAINER_HOSTNAME"
TEST_DATABASE_USER_ENV = "SMART_OA_MANAGER_REVIEW_TEST_DB_USER"
TEST_DATABASE_PREFIX = "smartoa_phase2step4_manager_"


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
        employee_no=f"MYSQLMGR{user_id}",
        username=f"mysql_manager_{user_id}",
        password_hash="unused-in-isolated-mysql-tests",
        name=f"MySQL Manager Review User {user_id}",
        role=role,
        department="Testing",
        manager_id=manager_id,
        is_active=True,
        must_change_password=False,
    )


class MySQLManagerReviewIdempotencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.getenv(TEST_DATABASE_ENV)
        expected_server_uuid = os.getenv(TEST_SERVER_UUID_ENV)
        expected_hostname = os.getenv(TEST_CONTAINER_HOSTNAME_ENV)
        expected_database_user = os.getenv(TEST_DATABASE_USER_ENV)
        if not all(
            (
                database_url,
                expected_server_uuid,
                expected_hostname,
                expected_database_user,
            )
        ):
            raise unittest.SkipTest(
                "Dedicated manager-review MySQL URL and identity bindings are required"
            )
        parsed = make_url(database_url)
        if parsed.drivername != "mysql+pymysql":
            raise RuntimeError("Integration URL must use mysql+pymysql")
        if parsed.host != "127.0.0.1" or parsed.port in {None, 3306}:
            raise RuntimeError("Refusing a non-loopback or default-port MySQL")
        if not (parsed.database or "").startswith(TEST_DATABASE_PREFIX):
            raise RuntimeError("Refusing to use a non-Step-4 database")
        if parsed.username != expected_database_user or not parsed.password:
            raise RuntimeError("Dedicated database account binding is invalid")
        try:
            UUID(expected_server_uuid)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Expected server UUID is invalid") from exc

        cls.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_timeout=5,
            pool_size=10,
            max_overflow=10,
        )
        try:
            with cls.engine.connect() as connection:
                identity = connection.execute(
                    text(
                        "SELECT @@server_uuid, @@hostname, DATABASE(), CURRENT_USER()"
                    )
                ).one()
            actual_user = str(identity[3]).split("@", 1)[0]
            if str(identity[0]).lower() != expected_server_uuid.lower():
                raise RuntimeError("MySQL server UUID mismatch")
            if str(identity[1]) != expected_hostname:
                raise RuntimeError("MySQL container hostname mismatch")
            if str(identity[2]) != parsed.database or actual_user != expected_database_user:
                raise RuntimeError("MySQL database or account mismatch")

            User.__table__.create(cls.engine, checkfirst=True)
            LeaveApplication.__table__.create(cls.engine, checkfirst=True)
            Notification.__table__.create(cls.engine, checkfirst=True)
            AuditLog.__table__.create(cls.engine, checkfirst=True)
            execute_operation_migration(cls.engine)
        except Exception:
            cls.engine.dispose()
            raise

        cls.server_identity = {
            "server_uuid": str(identity[0]),
            "hostname": str(identity[1]),
            "database": str(identity[2]),
            "database_user": actual_user,
            "host_port": parsed.port,
        }
        cls.SessionFactory = sessionmaker(
            bind=cls.engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        with cls.SessionFactory() as db:
            fixtures = (
                make_user(9601, role="manager"),
                make_user(9602, role="employee", manager_id=9601),
                make_user(9603, role="manager"),
                make_user(9604, role="hr"),
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

    def user(self, user_id: int) -> User:
        with self.SessionFactory() as db:
            item = db.get(User, user_id)
            self.assertIsNotNone(item)
            db.expunge(item)
            return item

    def application(self) -> LeaveApplication:
        item = LeaveApplication(
            id=str(uuid4()),
            employee_id=9602,
            manager_id=9601,
            leave_type="annual",
            start_date=date(2035, 5, 1),
            end_date=date(2035, 5, 2),
            leave_days=Decimal("2.0"),
            reason="isolated MySQL manager-review test",
            status="pending",
            manager_status="pending",
            hr_status="pending",
        )
        with self.SessionFactory() as db:
            db.add(item)
            db.commit()
            db.refresh(item)
            db.expunge(item)
        return item

    def payload(
        self,
        application: LeaveApplication,
        decision: str = "approved",
        comment: str | None = "looks good",
    ) -> dict:
        employee = self.user(9602)
        return {
            "application_id": application.id,
            "employee_name": employee.name,
            "employee_no": employee.employee_no,
            "leave_type": application.leave_type,
            "start_date": application.start_date.isoformat(),
            "end_date": application.end_date.isoformat(),
            "status": "pending",
            "decision": decision,
            "comment": comment,
        }

    def review(
        self,
        application: LeaveApplication,
        operation_id: str,
        *,
        decision: str = "approved",
        comment: str | None = "looks good",
    ) -> tuple[LeaveApplication, bool]:
        return leave_service.review_leave_by_manager_idempotent(
            self.SessionFactory,
            self.user(9601),
            application.id,
            LeaveReviewRequest(decision=decision, comment=comment),
            operation_id,
            self.payload(application, decision, comment),
            ip_address="127.0.0.1",
        )

    def effects(self, application_id: str) -> tuple[str, str, int, int]:
        with self.SessionFactory() as db:
            application = db.get(LeaveApplication, application_id)
            return (
                application.status,
                application.manager_status,
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

    def test_connection_is_bound_to_dedicated_container(self) -> None:
        self.assertNotEqual(self.server_identity["host_port"], 3306)
        self.assertTrue(self.server_identity["database"].startswith(TEST_DATABASE_PREFIX))
        self.assertEqual(
            self.server_identity["server_uuid"].lower(),
            os.environ[TEST_SERVER_UUID_ENV].lower(),
        )

    def test_approve_and_reject_persist_expected_states_and_notifications(self) -> None:
        approved = self.application()
        self.review(approved, str(uuid4()))
        self.assertEqual(self.effects(approved.id), ("pending", "approved", 1, 2))

        rejected = self.application()
        self.review(rejected, str(uuid4()), decision="rejected", comment="no")
        self.assertEqual(self.effects(rejected.id), ("rejected", "rejected", 1, 1))

    def test_success_replay_has_no_duplicate_effect(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        _first, first_replayed = self.review(application, operation_id)
        _second, replayed = self.review(application, operation_id)
        self.assertFalse(first_replayed)
        self.assertTrue(replayed)
        self.assertEqual(self.effects(application.id), ("pending", "approved", 1, 2))

    def test_concurrent_same_operation_has_one_business_effect(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        worker_count = 6
        barrier = Barrier(worker_count)
        counter_lock = Lock()
        active = 0
        maximum_active = 0

        def worker(_index: int) -> tuple[str, bool | None]:
            nonlocal active, maximum_active
            barrier.wait(timeout=10)
            with counter_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                try:
                    _result, replayed = self.review(application, operation_id)
                    return "ok", replayed
                except OperationInProgressError:
                    return "processing", None
            finally:
                with counter_lock:
                    active -= 1

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(executor.map(worker, range(worker_count)))
        self.assertGreaterEqual(maximum_active, 2)
        self.assertEqual(
            sum(1 for outcome, replayed in outcomes if outcome == "ok" and not replayed),
            1,
        )
        self.assertEqual(self.effects(application.id), ("pending", "approved", 1, 2))

    def test_row_lock_serializes_different_operations_for_same_application(self) -> None:
        application = self.application()
        operation_ids = (str(uuid4()), str(uuid4()))
        barrier = Barrier(2)

        def worker(operation_id: str) -> str:
            barrier.wait(timeout=10)
            try:
                self.review(application, operation_id)
                return "approved"
            except leave_service.LeaveConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(worker, operation_ids))
        self.assertEqual(sorted(outcomes), ["approved", "conflict"])
        self.assertEqual(self.effects(application.id), ("pending", "approved", 1, 2))
        with self.SessionFactory() as db:
            statuses = {
                db.get(BusinessOperation, operation_id).status
                for operation_id in operation_ids
            }
        self.assertEqual(statuses, {"SUCCEEDED", "FAILED"})

    def test_failure_before_commit_is_atomic_and_failed(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        with (
            patch(
                "app.services.leave_service._before_idempotent_commit",
                side_effect=ConnectionError("injected before commit"),
            ),
            self.assertRaisesRegex(ConnectionError, "injected before commit"),
        ):
            self.review(application, operation_id)
        self.assertEqual(self.effects(application.id), ("pending", "pending", 0, 0))
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, operation_id)
            self.assertEqual(operation.status, "FAILED")
            self.assertEqual(operation.error_code, "EXECUTION_ABORTED")

    def test_processing_and_unknown_do_not_execute_review(self) -> None:
        for status, expected_error in (
            ("PROCESSING", OperationInProgressError),
            ("UNKNOWN", OperationResultUnknownError),
        ):
            with self.subTest(status=status):
                application = self.application()
                operation_id = str(uuid4())
                with self.SessionFactory() as db:
                    operation, _ = get_or_create_operation_record(
                        db,
                        operation_id=operation_id,
                        actor_user_id=9601,
                        operation_type="MANAGER_REVIEW",
                        payload=self.payload(application),
                    )
                    if status == "UNKNOWN":
                        transition_processing_operation(
                            operation,
                            "UNKNOWN",
                            error_code="COMMIT_RESULT_UNKNOWN",
                        )
                    db.commit()
                with self.assertRaises(expected_error):
                    self.review(application, operation_id)
                self.assertEqual(
                    self.effects(application.id),
                    ("pending", "pending", 0, 0),
                )


if __name__ == "__main__":
    unittest.main()
