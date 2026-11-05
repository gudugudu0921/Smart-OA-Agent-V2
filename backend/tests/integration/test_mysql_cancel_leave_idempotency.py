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
from app.services import leave_service
from app.services.idempotency_service import (
    IdempotencyConflictError,
    OperationInProgressError,
    OperationNotFoundError,
    OperationResultUnknownError,
    get_or_create_operation_record,
    transition_processing_operation,
)


TEST_DATABASE_ENV = "SMART_OA_CANCEL_LEAVE_TEST_DATABASE_URL"
TEST_SERVER_UUID_ENV = "SMART_OA_CANCEL_LEAVE_TEST_SERVER_UUID"
TEST_CONTAINER_HOSTNAME_ENV = "SMART_OA_CANCEL_LEAVE_TEST_CONTAINER_HOSTNAME"
TEST_DATABASE_USER_ENV = "SMART_OA_CANCEL_LEAVE_TEST_DB_USER"
TEST_DATABASE_PREFIX = "smartoa_phase2step31_cancel_"


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
        employee_no=f"MYSQLCANCEL{user_id}",
        username=f"mysql_cancel_{user_id}",
        password_hash="unused-in-isolated-mysql-tests",
        name=f"MySQL Cancel Leave User {user_id}",
        role=role,
        department="Testing",
        manager_id=manager_id,
        is_active=True,
        must_change_password=False,
    )


class MySQLCancelLeaveIdempotencyTests(unittest.TestCase):
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
                "Dedicated cancel-leave MySQL URL and identity bindings are required"
            )

        parsed = make_url(database_url)
        if parsed.drivername != "mysql+pymysql":
            raise RuntimeError("Integration URL must use mysql+pymysql")
        if parsed.host != "127.0.0.1":
            raise RuntimeError("Integration MySQL must bind to IPv4 loopback")
        if parsed.port in {None, 3306}:
            raise RuntimeError("Refusing the default or unspecified MySQL port")
        if not (parsed.database or "").startswith(TEST_DATABASE_PREFIX):
            raise RuntimeError("Refusing to use a non-Step-3.1 database")
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
            pool_size=8,
            max_overflow=8,
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
                raise RuntimeError("MySQL server UUID does not match the dedicated container")
            if str(identity[1]) != expected_hostname:
                raise RuntimeError("MySQL hostname does not match the dedicated container")
            if str(identity[2]) != parsed.database:
                raise RuntimeError("MySQL selected an unexpected database")
            if actual_user != expected_database_user:
                raise RuntimeError("MySQL authenticated an unexpected account")

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
                make_user(9501, role="manager"),
                make_user(9502, role="employee", manager_id=9501),
                make_user(9503, role="employee", manager_id=9501),
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
            user = db.get(User, user_id)
            self.assertIsNotNone(user)
            db.expunge(user)
            return user

    def application(
        self,
        *,
        employee_id: int = 9502,
        status: str = "pending",
        manager_status: str = "pending",
    ) -> LeaveApplication:
        application = LeaveApplication(
            id=str(uuid4()),
            employee_id=employee_id,
            leave_type="annual",
            start_date=date(2034, 4, 1),
            end_date=date(2034, 4, 2),
            leave_days=Decimal("2.0"),
            reason="isolated MySQL cancel-leave test",
            status=status,
            manager_status=manager_status,
            manager_id=9501,
            hr_status="pending",
        )
        with self.SessionFactory() as db:
            db.add(application)
            db.commit()
            db.refresh(application)
            db.expunge(application)
        return application

    @staticmethod
    def payload(application: LeaveApplication) -> dict[str, str]:
        return {
            "application_id": application.id,
            "leave_type": application.leave_type,
            "start_date": application.start_date.isoformat(),
            "end_date": application.end_date.isoformat(),
            "status": "pending",
        }

    def cancel(
        self,
        application: LeaveApplication,
        operation_id: str,
        *,
        employee_id: int = 9502,
        payload: dict[str, str] | None = None,
    ) -> tuple[LeaveApplication, bool]:
        return leave_service.cancel_leave_application_idempotent(
            self.SessionFactory,
            self.user(employee_id),
            application.id,
            operation_id,
            payload or self.payload(application),
            ip_address="127.0.0.1",
        )

    def reserve(
        self,
        application: LeaveApplication,
        operation_id: str,
        *,
        status: str = "PROCESSING",
    ) -> None:
        with self.SessionFactory() as db:
            operation, _created = get_or_create_operation_record(
                db,
                operation_id=operation_id,
                actor_user_id=9502,
                operation_type="CANCEL_LEAVE",
                payload=self.payload(application),
            )
            if status == "UNKNOWN":
                transition_processing_operation(
                    operation,
                    "UNKNOWN",
                    error_code="COMMIT_RESULT_UNKNOWN",
                )
            db.commit()

    def effects(self, application_id: str) -> tuple[str, int, int]:
        with self.SessionFactory() as db:
            application = db.get(LeaveApplication, application_id)
            return (
                application.status,
                db.query(AuditLog)
                .filter(
                    AuditLog.action == "CANCEL_LEAVE",
                    AuditLog.resource_id == application_id,
                )
                .count(),
                db.query(Notification)
                .filter(Notification.related_application_id == application_id)
                .count(),
            )

    def test_connection_is_bound_to_dedicated_container_identity(self) -> None:
        self.assertNotEqual(self.server_identity["host_port"], 3306)
        self.assertTrue(
            self.server_identity["database"].startswith(TEST_DATABASE_PREFIX)
        )
        self.assertEqual(
            self.server_identity["database_user"],
            os.environ[TEST_DATABASE_USER_ENV],
        )
        self.assertEqual(
            self.server_identity["hostname"],
            os.environ[TEST_CONTAINER_HOSTNAME_ENV],
        )
        self.assertEqual(
            self.server_identity["server_uuid"].lower(),
            os.environ[TEST_SERVER_UUID_ENV].lower(),
        )

    def test_success_and_same_operation_replay_have_one_effect(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        first, first_replayed = self.cancel(application, operation_id)
        replay, replayed = self.cancel(application, operation_id)
        self.assertEqual(first.id, replay.id)
        self.assertFalse(first_replayed)
        self.assertTrue(replayed)
        self.assertEqual(self.effects(application.id), ("cancelled", 1, 0))
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, operation_id)
            self.assertEqual(operation.status, "SUCCEEDED")
            self.assertEqual(operation.resource_id, application.id)

    def test_other_owner_and_non_cancellable_status_are_rejected(self) -> None:
        owned = self.application()
        other_operation = str(uuid4())
        with self.assertRaises(leave_service.LeaveNotFoundError):
            self.cancel(owned, other_operation, employee_id=9503)
        self.assertEqual(self.effects(owned.id), ("pending", 0, 0))

        reviewed = self.application(status="manager_approved", manager_status="approved")
        reviewed_operation = str(uuid4())
        with self.assertRaises(leave_service.LeaveConflictError):
            self.cancel(reviewed, reviewed_operation)
        self.assertEqual(self.effects(reviewed.id), ("manager_approved", 0, 0))
        with self.SessionFactory() as db:
            self.assertEqual(db.get(BusinessOperation, other_operation).status, "FAILED")
            self.assertEqual(db.get(BusinessOperation, reviewed_operation).status, "FAILED")

    def test_same_operation_rejects_different_target_and_actor(self) -> None:
        first = self.application()
        second = self.application()
        operation_id = str(uuid4())
        self.cancel(first, operation_id)
        with self.assertRaises(IdempotencyConflictError):
            self.cancel(second, operation_id)
        with self.assertRaises(OperationNotFoundError):
            self.cancel(second, operation_id, employee_id=9503)
        self.assertEqual(self.effects(first.id), ("cancelled", 1, 0))
        self.assertEqual(self.effects(second.id), ("pending", 0, 0))

    def test_concurrent_same_operation_has_one_business_effect(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        worker_count = 6
        barrier = Barrier(worker_count)
        active_lock = Lock()
        active = 0
        maximum_active = 0

        def worker(_index: int) -> tuple[str, bool | None]:
            nonlocal active, maximum_active
            barrier.wait(timeout=10)
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                try:
                    _result, replayed = self.cancel(application, operation_id)
                    return "ok", replayed
                except OperationInProgressError:
                    return "processing", None
            finally:
                with active_lock:
                    active -= 1

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(executor.map(worker, range(worker_count)))
        self.assertGreaterEqual(maximum_active, 2)
        self.assertEqual(
            sum(1 for outcome, replayed in outcomes if outcome == "ok" and not replayed),
            1,
        )
        self.assertEqual(self.effects(application.id), ("cancelled", 1, 0))
        with self.SessionFactory() as db:
            self.assertEqual(
                db.query(BusinessOperation)
                .filter(BusinessOperation.operation_id == operation_id)
                .count(),
                1,
            )
            self.assertEqual(db.get(BusinessOperation, operation_id).status, "SUCCEEDED")

    def test_row_lock_serializes_different_operations_for_same_application(self) -> None:
        application = self.application()
        operation_ids = (str(uuid4()), str(uuid4()))
        barrier = Barrier(2)

        def worker(operation_id: str) -> str:
            barrier.wait(timeout=10)
            try:
                self.cancel(application, operation_id)
                return "cancelled"
            except leave_service.LeaveConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(worker, operation_ids))
        self.assertEqual(sorted(outcomes), ["cancelled", "conflict"])
        self.assertEqual(self.effects(application.id), ("cancelled", 1, 0))
        with self.SessionFactory() as db:
            statuses = {
                db.get(BusinessOperation, operation_id).status
                for operation_id in operation_ids
            }
        self.assertEqual(statuses, {"SUCCEEDED", "FAILED"})

    def test_failure_before_commit_rolls_back_business_and_records_failed(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        with (
            patch(
                "app.services.leave_service._before_idempotent_commit",
                side_effect=ConnectionError("injected before commit"),
            ),
            self.assertRaisesRegex(ConnectionError, "injected before commit"),
        ):
            self.cancel(application, operation_id)
        self.assertEqual(self.effects(application.id), ("pending", 0, 0))
        with self.SessionFactory() as db:
            operation = db.get(BusinessOperation, operation_id)
            self.assertEqual(operation.status, "FAILED")
            self.assertEqual(operation.error_code, "EXECUTION_ABORTED")

    def test_commit_success_with_lost_response_recovers_without_duplication(self) -> None:
        application = self.application()
        operation_id = str(uuid4())
        real_commit = leave_service._commit_idempotent_transaction

        def commit_then_lose_response(db) -> None:
            real_commit(db)
            raise ConnectionError("injected response loss")

        with patch(
            "app.services.leave_service._commit_idempotent_transaction",
            side_effect=commit_then_lose_response,
        ):
            recovered, recovered_as_replay = self.cancel(application, operation_id)
        replay, replayed = self.cancel(application, operation_id)
        self.assertEqual(recovered.id, replay.id)
        self.assertTrue(recovered_as_replay)
        self.assertTrue(replayed)
        self.assertEqual(self.effects(application.id), ("cancelled", 1, 0))

    def test_processing_and_unknown_never_execute_business_mutation(self) -> None:
        processing_application = self.application()
        processing_id = str(uuid4())
        self.reserve(processing_application, processing_id)
        with self.assertRaises(OperationInProgressError):
            self.cancel(processing_application, processing_id)
        self.assertEqual(self.effects(processing_application.id), ("pending", 0, 0))

        unknown_application = self.application()
        unknown_id = str(uuid4())
        self.reserve(unknown_application, unknown_id, status="UNKNOWN")
        with self.assertRaises(OperationResultUnknownError):
            self.cancel(unknown_application, unknown_id)
        self.assertEqual(self.effects(unknown_application.id), ("pending", 0, 0))


if __name__ == "__main__":
    unittest.main()
