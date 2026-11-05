from __future__ import annotations

import math
import unittest
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import DefaultClause, MetaData, create_engine, event, text
from sqlalchemy.orm import Session

from app.models.business_operation import BusinessOperation
from app.models.user import User
from app.services.idempotency_service import (
    IdempotencyConflictError,
    InvalidIdempotencyInputError,
    OperationNotFoundError,
    calculate_request_fingerprint,
    canonical_payload_json,
    get_operation_for_actor,
    get_or_create_operation_record,
    normalize_uuid,
    set_operation_status,
)


class ExampleDecision(str, Enum):
    APPROVED = "approved"


def make_user(user_id: int) -> User:
    return User(
        id=user_id,
        employee_no=f"IDEM{user_id:04d}",
        username=f"idem_{user_id}",
        password_hash="unused-in-idempotency-tests",
        name=f"Idempotency User {user_id}",
        role="employee",
        department="Testing",
        is_active=True,
        must_change_password=False,
    )


class FingerprintTests(unittest.TestCase):
    def test_canonical_payload_is_order_independent_and_type_bound(self) -> None:
        first = {
            "end_date": date(2030, 5, 2),
            "start_date": date(2030, 5, 1),
            "reason": None,
        }
        second = {
            "reason": None,
            "start_date": date(2030, 5, 1),
            "end_date": date(2030, 5, 2),
        }
        self.assertEqual(
            calculate_request_fingerprint("SUBMIT_LEAVE", first),
            calculate_request_fingerprint("SUBMIT_LEAVE", second),
        )
        self.assertNotEqual(
            calculate_request_fingerprint("SUBMIT_LEAVE", first),
            calculate_request_fingerprint("CANCEL_LEAVE", first),
        )

    def test_uuid_date_enum_datetime_and_null_are_stably_normalized(self) -> None:
        application_id = UUID("3B241101-E2BB-4255-8CAF-4136C566A962")
        canonical = canonical_payload_json(
            "MANAGER_REVIEW",
            {
                "application_id": application_id,
                "decision": ExampleDecision.APPROVED,
                "reviewed_at": datetime(2030, 1, 2, 10, 30, tzinfo=UTC),
                "comment": None,
            },
        )
        self.assertIn('"application_id":"3b241101-e2bb-4255-8caf-4136c566a962"', canonical)
        self.assertIn('"decision":"approved"', canonical)
        self.assertIn('"reviewed_at":"2030-01-02T10:30:00Z"', canonical)
        self.assertIn('"comment":null', canonical)
        self.assertEqual(
            normalize_uuid("3B241101-E2BB-4255-8CAF-4136C566A962", field_name="id"),
            "3b241101-e2bb-4255-8caf-4136c566a962",
        )
        uppercase_string = calculate_request_fingerprint(
            "CANCEL_LEAVE",
            {"application_id": "3B241101-E2BB-4255-8CAF-4136C566A962"},
        )
        lowercase_string = calculate_request_fingerprint(
            "CANCEL_LEAVE",
            {"application_id": "3b241101-e2bb-4255-8caf-4136c566a962"},
        )
        self.assertEqual(uppercase_string, lowercase_string)

    def test_ambiguous_or_non_json_values_are_rejected(self) -> None:
        with self.assertRaises(InvalidIdempotencyInputError):
            calculate_request_fingerprint(
                "SUBMIT_LEAVE", {"timestamp": datetime(2030, 1, 1)}
            )
        with self.assertRaises(InvalidIdempotencyInputError):
            calculate_request_fingerprint("SUBMIT_LEAVE", {"value": math.nan})
        with self.assertRaises(InvalidIdempotencyInputError):
            calculate_request_fingerprint("UNSUPPORTED", {})


class IdempotencyRecordUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

        metadata = MetaData()
        for table in (User.__table__, BusinessOperation.__table__):
            clone = table.to_metadata(metadata)
            for column in clone.columns:
                if (
                    column.server_default is not None
                    and "ON UPDATE" in str(column.server_default.arg)
                ):
                    column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
        metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.db.add_all([make_user(101), make_user(102)])
        self.db.commit()
        self.operation_id = str(uuid4())
        self.payload = {
            "leave_type": "annual",
            "start_date": date(2030, 5, 1),
            "end_date": date(2030, 5, 2),
            "reason": None,
        }

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_same_key_actor_type_and_payload_returns_existing_record(self) -> None:
        first, created = get_or_create_operation_record(
            self.db,
            operation_id=self.operation_id,
            actor_user_id=101,
            operation_type="SUBMIT_LEAVE",
            payload=self.payload,
        )
        self.db.commit()
        second, second_created = get_or_create_operation_record(
            self.db,
            operation_id=self.operation_id,
            actor_user_id=101,
            operation_type="SUBMIT_LEAVE",
            payload=dict(reversed(list(self.payload.items()))),
        )
        self.assertTrue(created)
        self.assertFalse(second_created)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(second.status, "PROCESSING")

    def test_same_key_with_different_payload_or_type_is_a_conflict(self) -> None:
        get_or_create_operation_record(
            self.db,
            operation_id=self.operation_id,
            actor_user_id=101,
            operation_type="SUBMIT_LEAVE",
            payload=self.payload,
        )
        self.db.commit()
        with self.assertRaises(IdempotencyConflictError):
            get_or_create_operation_record(
                self.db,
                operation_id=self.operation_id,
                actor_user_id=101,
                operation_type="SUBMIT_LEAVE",
                payload={**self.payload, "reason": "different"},
            )
        with self.assertRaises(IdempotencyConflictError):
            get_or_create_operation_record(
                self.db,
                operation_id=self.operation_id,
                actor_user_id=101,
                operation_type="CANCEL_LEAVE",
                payload=self.payload,
            )

    def test_other_actor_cannot_reuse_or_read_the_operation(self) -> None:
        get_or_create_operation_record(
            self.db,
            operation_id=self.operation_id,
            actor_user_id=101,
            operation_type="SUBMIT_LEAVE",
            payload=self.payload,
        )
        self.db.commit()
        with self.assertRaises(OperationNotFoundError):
            get_or_create_operation_record(
                self.db,
                operation_id=self.operation_id,
                actor_user_id=102,
                operation_type="SUBMIT_LEAVE",
                payload=self.payload,
            )
        with self.assertRaises(OperationNotFoundError):
            get_operation_for_actor(self.db, self.operation_id, 102)

    def test_all_foundation_statuses_and_json_results_persist(self) -> None:
        for status in ("PROCESSING", "SUCCEEDED", "FAILED", "UNKNOWN"):
            operation, _ = get_or_create_operation_record(
                self.db,
                operation_id=str(uuid4()),
                actor_user_id=101,
                operation_type="SUBMIT_LEAVE",
                payload={**self.payload, "status_case": status},
            )
            set_operation_status(
                operation,
                status,
                resource_type="leave_application" if status == "SUCCEEDED" else None,
                resource_id=str(uuid4()) if status == "SUCCEEDED" else None,
                result_payload={"success": status == "SUCCEEDED", "value": None},
                error_code="TEST_FAILURE" if status in {"FAILED", "UNKNOWN"} else None,
            )
        self.db.commit()
        saved = {
            item.status: item
            for item in self.db.query(BusinessOperation).order_by(
                BusinessOperation.created_at
            )
        }
        self.assertEqual(set(saved), {"PROCESSING", "SUCCEEDED", "FAILED", "UNKNOWN"})
        self.assertEqual(saved["SUCCEEDED"].result_payload["success"], True)
        self.assertIsNotNone(saved["SUCCEEDED"].completed_at)
        self.assertIsNone(saved["PROCESSING"].completed_at)

    def test_transaction_rollback_removes_uncommitted_record(self) -> None:
        get_or_create_operation_record(
            self.db,
            operation_id=self.operation_id,
            actor_user_id=101,
            operation_type="SUBMIT_LEAVE",
            payload=self.payload,
        )
        self.db.rollback()
        self.assertIsNone(self.db.get(BusinessOperation, self.operation_id))

    def test_result_payload_has_a_bounded_json_size(self) -> None:
        operation, _ = get_or_create_operation_record(
            self.db,
            operation_id=self.operation_id,
            actor_user_id=101,
            operation_type="SUBMIT_LEAVE",
            payload=self.payload,
        )
        with self.assertRaises(InvalidIdempotencyInputError):
            set_operation_status(
                operation,
                "SUCCEEDED",
                result_payload={"value": "x" * (64 * 1024)},
            )

    def test_migration_only_creates_the_new_table(self) -> None:
        migration = (
            Path(__file__).parents[1]
            / "app"
            / "db"
            / "migrations"
            / "phase2_business_operations.sql"
        ).read_text(encoding="utf-8")
        upper = migration.upper()
        self.assertIn("CREATE TABLE IF NOT EXISTS BUSINESS_OPERATIONS", upper)
        for legacy_table in (
            "USERS",
            "LEAVE_APPLICATIONS",
            "NOTIFICATIONS",
            "AUDIT_LOGS",
        ):
            self.assertNotIn(f"ALTER TABLE {legacy_table}", upper)
            self.assertNotIn(f"DROP TABLE {legacy_table}", upper)


if __name__ == "__main__":
    unittest.main()
