from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models.business_operation import BusinessOperation
from app.models.user import User
from app.services.idempotency_service import (
    IdempotencyConflictError,
    OperationNotFoundError,
    get_operation_for_actor,
    get_or_create_operation_record,
    set_operation_status,
)


TEST_DATABASE_ENV = "SMART_OA_IDEMPOTENCY_TEST_DATABASE_URL"
TEST_DATABASE_PREFIX = "smartoa_phase2a1_"


def execute_migration(engine) -> None:
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


def make_user(user_id: int) -> User:
    return User(
        id=user_id,
        employee_no=f"MYSQLIDEM{user_id}",
        username=f"mysql_idem_{user_id}",
        password_hash="unused-in-isolated-mysql-tests",
        name=f"MySQL Idempotency User {user_id}",
        role="employee",
        department="Testing",
        is_active=True,
        must_change_password=False,
    )


class MySQLIdempotencyIntegrationTests(unittest.TestCase):
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
            raise RuntimeError("Refusing to use a non-Phase-2A-1 database")

        cls.engine = create_engine(database_url, pool_pre_ping=True, pool_timeout=3)
        User.__table__.create(cls.engine, checkfirst=True)
        execute_migration(cls.engine)
        execute_migration(cls.engine)
        cls.SessionFactory = sessionmaker(
            bind=cls.engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        with cls.SessionFactory() as db:
            for user_id in (9101, 9102):
                if db.get(User, user_id) is None:
                    db.add(make_user(user_id))
            db.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM business_operations"))
        self.payload = {
            "leave_type": "annual",
            "start_date": date(2031, 3, 1),
            "end_date": date(2031, 3, 2),
            "reason": None,
        }

    def test_migration_is_repeatable_and_schema_has_expected_constraints(self) -> None:
        execute_migration(self.engine)
        inspector = inspect(self.engine)
        columns = {item["name"]: item for item in inspector.get_columns("business_operations")}
        indexes = {item["name"] for item in inspector.get_indexes("business_operations")}
        checks = {item["name"] for item in inspector.get_check_constraints("business_operations")}
        self.assertEqual(inspector.get_pk_constraint("business_operations")["constrained_columns"], ["operation_id"])
        self.assertEqual(columns["actor_user_id"]["type"].__class__.__name__, "INTEGER")
        self.assertFalse(columns["actor_user_id"]["nullable"])
        self.assertEqual(columns["result_payload"]["type"].__class__.__name__, "JSON")
        self.assertTrue(
            {
                "idx_business_operations_actor_created",
                "idx_business_operations_status_lease",
                "idx_business_operations_resource",
            }.issubset(indexes)
        )
        self.assertTrue(
            {
                "chk_business_operations_type",
                "chk_business_operations_status",
                "chk_business_operations_fingerprint_version",
                "chk_business_operations_attempt_count",
            }.issubset(checks)
        )

    def test_insert_read_conflict_and_actor_boundary(self) -> None:
        operation_id = str(uuid4())
        with self.SessionFactory() as db:
            operation, created = get_or_create_operation_record(
                db,
                operation_id=operation_id,
                actor_user_id=9101,
                operation_type="SUBMIT_LEAVE",
                payload=self.payload,
            )
            db.commit()
            self.assertTrue(created)
            self.assertEqual(operation.status, "PROCESSING")

        with self.SessionFactory() as db:
            operation, created = get_or_create_operation_record(
                db,
                operation_id=operation_id,
                actor_user_id=9101,
                operation_type="SUBMIT_LEAVE",
                payload=dict(reversed(list(self.payload.items()))),
            )
            self.assertFalse(created)
            self.assertEqual(operation.actor_user_id, 9101)
            with self.assertRaises(IdempotencyConflictError):
                get_or_create_operation_record(
                    db,
                    operation_id=operation_id,
                    actor_user_id=9101,
                    operation_type="SUBMIT_LEAVE",
                    payload={**self.payload, "reason": "changed"},
                )
            with self.assertRaises(IdempotencyConflictError):
                get_or_create_operation_record(
                    db,
                    operation_id=operation_id,
                    actor_user_id=9101,
                    operation_type="CANCEL_LEAVE",
                    payload=self.payload,
                )
            with self.assertRaises(OperationNotFoundError):
                get_operation_for_actor(db, operation_id, 9102)

    def test_database_primary_key_rejects_direct_duplicates(self) -> None:
        operation_id = str(uuid4())
        with self.SessionFactory() as db:
            operation, _ = get_or_create_operation_record(
                db,
                operation_id=operation_id,
                actor_user_id=9101,
                operation_type="SUBMIT_LEAVE",
                payload=self.payload,
            )
            db.commit()
            operation_type = operation.operation_type
            fingerprint_version = operation.fingerprint_version
            request_fingerprint = operation.request_fingerprint

        with self.SessionFactory() as db:
            duplicate = BusinessOperation(
                operation_id=operation_id,
                actor_user_id=9101,
                operation_type=operation_type,
                fingerprint_version=fingerprint_version,
                request_fingerprint=request_fingerprint,
                status="PROCESSING",
            )
            db.add(duplicate)
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_all_statuses_and_json_results_persist_in_mysql(self) -> None:
        ids: dict[str, str] = {}
        with self.SessionFactory() as db:
            for status in ("PROCESSING", "SUCCEEDED", "FAILED", "UNKNOWN"):
                ids[status] = str(uuid4())
                operation, _ = get_or_create_operation_record(
                    db,
                    operation_id=ids[status],
                    actor_user_id=9101,
                    operation_type="SUBMIT_LEAVE",
                    payload={**self.payload, "status_case": status},
                )
                set_operation_status(
                    operation,
                    status,
                    result_payload={"success": status == "SUCCEEDED", "data": None},
                    error_code="TEST_FAILURE" if status in {"FAILED", "UNKNOWN"} else None,
                )
            db.commit()
        with self.SessionFactory() as db:
            for status, operation_id in ids.items():
                saved = db.get(BusinessOperation, operation_id)
                self.assertIsNotNone(saved)
                self.assertEqual(saved.status, status)
                self.assertEqual(saved.result_payload["success"], status == "SUCCEEDED")

    def test_transaction_rollback_leaves_no_record(self) -> None:
        operation_id = str(uuid4())
        with self.SessionFactory() as db:
            get_or_create_operation_record(
                db,
                operation_id=operation_id,
                actor_user_id=9101,
                operation_type="SUBMIT_LEAVE",
                payload=self.payload,
            )
            db.rollback()
        with self.SessionFactory() as db:
            self.assertIsNone(db.get(BusinessOperation, operation_id))

    def test_concurrent_same_key_creates_exactly_one_record(self) -> None:
        operation_id = str(uuid4())
        worker_count = 6
        barrier = Barrier(worker_count)

        def reserve(_index: int) -> bool:
            with self.SessionFactory() as db:
                barrier.wait(timeout=10)
                _operation, created = get_or_create_operation_record(
                    db,
                    operation_id=operation_id,
                    actor_user_id=9101,
                    operation_type="SUBMIT_LEAVE",
                    payload=self.payload,
                )
                db.commit()
                return created

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            created_flags = list(executor.map(reserve, range(worker_count)))
        self.assertEqual(created_flags.count(True), 1)
        with self.SessionFactory() as db:
            count = (
                db.query(BusinessOperation)
                .filter(BusinessOperation.operation_id == operation_id)
                .count()
            )
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
