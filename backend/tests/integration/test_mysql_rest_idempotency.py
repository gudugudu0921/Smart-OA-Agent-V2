from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.api.routes.leave import get_idempotency_session_factory
from app.core.dependencies import get_current_business_user
from app.db.mysql import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.business_operation import BusinessOperation
from app.models.leave_application import LeaveApplication
from app.models.notification import Notification
from app.models.user import User
from app.schemas.leave import LeaveCreate
from app.services.idempotency_service import (
    get_or_create_operation_record,
    transition_processing_operation,
)


TEST_DATABASE_ENV = "SMART_OA_REST_IDEMPOTENCY_TEST_DATABASE_URL"
TEST_DATABASE_PREFIX = "smartoa_phase2a3_"
TEST_USER_HEADER = "X-Test-User-ID"


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
        employee_no=f"MYSQLREST{user_id}",
        username=f"mysql_rest_{user_id}",
        password_hash="unused-in-isolated-mysql-tests",
        name=f"MySQL REST User {user_id}",
        role=role,
        department="Testing",
        manager_id=manager_id,
        is_active=True,
        must_change_password=False,
    )


class MySQLRestIdempotencyTests(unittest.TestCase):
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
            raise RuntimeError("Refusing to use a non-Phase-2A-3 database")

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
                make_user(9401, role="manager"),
                make_user(9402, role="employee", manager_id=9401),
                make_user(9403, role="employee", manager_id=9401),
            )
            for fixture in fixtures:
                if db.get(User, fixture.id) is None:
                    db.add(fixture)
            db.commit()

        def override_get_db():
            db = cls.SessionFactory()
            try:
                yield db
            finally:
                db.close()

        def override_current_user(request: Request) -> User:
            user_id = int(request.headers.get(TEST_USER_HEADER, "9402"))
            with cls.SessionFactory() as db:
                user = db.get(User, user_id)
                if user is None:
                    raise RuntimeError("Missing isolated test user")
                db.expunge(user)
                return user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_business_user] = override_current_user
        app.dependency_overrides[get_idempotency_session_factory] = (
            lambda: cls.SessionFactory
        )
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM notifications"))
            connection.execute(text("DELETE FROM audit_logs"))
            connection.execute(text("DELETE FROM leave_applications"))
            connection.execute(text("DELETE FROM business_operations"))
        self.payload = {
            "leave_type": "annual",
            "start_date": "2033-05-01",
            "end_date": "2033-05-02",
            "reason": "isolated REST idempotency test",
        }

    def post_leave(
        self,
        *,
        operation_id: str | None = None,
        payload: dict | None = None,
        user_id: int = 9402,
        client: TestClient | None = None,
    ):
        headers = {TEST_USER_HEADER: str(user_id), "X-Request-ID": "phase2a3-test"}
        if operation_id is not None:
            headers["Idempotency-Key"] = operation_id
        return (client or self.client).post(
            "/leave/",
            json=payload or self.payload,
            headers=headers,
        )

    def get_operation(self, operation_id: str, *, user_id: int = 9402):
        return self.client.get(
            f"/operations/{operation_id}",
            headers={TEST_USER_HEADER: str(user_id), "X-Request-ID": "phase2a3-test"},
        )

    def counts(self) -> tuple[int, int, int]:
        with self.SessionFactory() as db:
            return (
                db.query(LeaveApplication).count(),
                db.query(AuditLog).filter(AuditLog.action == "CREATE_LEAVE").count(),
                db.query(Notification).filter(Notification.type == "leave_created").count(),
            )

    def reserve(
        self,
        operation_id: str,
        *,
        status: str = "PROCESSING",
        operation_type: str = "SUBMIT_LEAVE",
        error_code: str | None = None,
    ) -> None:
        payload = LeaveCreate.model_validate(self.payload).model_dump(mode="python")
        with self.SessionFactory() as db:
            operation, _created = get_or_create_operation_record(
                db,
                operation_id=operation_id,
                actor_user_id=9402,
                operation_type=operation_type,
                payload=payload,
            )
            if status != "PROCESSING":
                transition_processing_operation(
                    operation,
                    status,
                    error_code=error_code,
                )
            db.commit()

    def test_legacy_post_without_header_preserves_original_contract(self) -> None:
        response = self.post_leave()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["employee_id"], 9402)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.SessionFactory() as db:
            self.assertEqual(db.query(BusinessOperation).count(), 0)

    def test_first_post_and_replay_return_same_application_once(self) -> None:
        operation_id = str(uuid4())
        first = self.post_leave(operation_id=operation_id)
        replay = self.post_leave(operation_id=operation_id)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_same_key_different_payload_is_a_stable_conflict(self) -> None:
        operation_id = str(uuid4())
        self.assertEqual(self.post_leave(operation_id=operation_id).status_code, 201)
        changed = {**self.payload, "reason": "changed"}
        response = self.post_leave(operation_id=operation_id, payload=changed)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(response.headers["X-Request-ID"], "phase2a3-test")
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_different_user_cannot_reuse_or_query_the_operation(self) -> None:
        operation_id = str(uuid4())
        self.assertEqual(self.post_leave(operation_id=operation_id).status_code, 201)
        replay = self.post_leave(operation_id=operation_id, user_id=9403)
        query = self.get_operation(operation_id, user_id=9403)
        self.assertEqual(replay.status_code, 404)
        self.assertEqual(query.status_code, 404)
        self.assertEqual(replay.json()["code"], "OPERATION_NOT_FOUND")
        self.assertEqual(query.json()["code"], "OPERATION_NOT_FOUND")
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_non_employee_cannot_create_leave(self) -> None:
        response = self.post_leave(operation_id=str(uuid4()), user_id=9401)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "PERMISSION_DENIED")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_succeeded_operation_query_has_only_safe_fields(self) -> None:
        operation_id = str(uuid4())
        created = self.post_leave(operation_id=operation_id)
        response = self.get_operation(operation_id)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            set(body),
            {
                "operation_id",
                "operation_type",
                "status",
                "resource_id",
                "result_payload",
                "error_code",
            },
        )
        self.assertEqual(body["status"], "SUCCEEDED")
        self.assertEqual(body["resource_id"], created.json()["id"])
        self.assertEqual(body["result_payload"], {"application_id": body["resource_id"]})

    def test_processing_failed_and_unknown_have_stable_post_and_get_semantics(self) -> None:
        cases = (
            ("PROCESSING", None, "OPERATION_IN_PROGRESS"),
            ("FAILED", "LEAVE_CONFLICT", "OPERATION_FAILED"),
            ("UNKNOWN", "COMMIT_RESULT_UNKNOWN", "OPERATION_RESULT_UNKNOWN"),
        )
        for state, error_code, post_code in cases:
            operation_id = str(uuid4())
            self.reserve(operation_id, status=state, error_code=error_code)
            post_response = self.post_leave(operation_id=operation_id)
            get_response = self.get_operation(operation_id)
            self.assertEqual(post_response.status_code, 409)
            self.assertEqual(post_response.json()["code"], post_code)
            self.assertEqual(get_response.status_code, 200)
            self.assertEqual(get_response.json()["status"], state)
            self.assertEqual(get_response.json()["error_code"], error_code)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_invalid_header_and_path_uuid_are_safe_validation_errors(self) -> None:
        post_response = self.post_leave(operation_id="not-a-uuid")
        get_response = self.get_operation("not-a-uuid")
        self.assertEqual(post_response.status_code, 422)
        self.assertEqual(get_response.status_code, 422)
        self.assertEqual(post_response.json()["code"], "VALIDATION_ERROR")
        self.assertEqual(get_response.json()["code"], "VALIDATION_ERROR")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_discarded_success_response_is_recoverable_via_get(self) -> None:
        operation_id = str(uuid4())
        discarded_response = self.post_leave(operation_id=operation_id)
        self.assertEqual(discarded_response.status_code, 201)
        recovered = self.get_operation(operation_id)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["status"], "SUCCEEDED")
        self.assertEqual(
            recovered.json()["resource_id"],
            discarded_response.json()["id"],
        )
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_same_key_rest_posts_create_once(self) -> None:
        operation_id = str(uuid4())
        worker_count = 6
        barrier = Barrier(worker_count)

        def worker(_index: int):
            barrier.wait(timeout=10)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = self.post_leave(operation_id=operation_id, client=client)
                return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(worker, range(worker_count)))
        self.assertTrue(any(status == 201 for status, _body in results))
        self.assertTrue(all(status in {201, 409} for status, _body in results))
        successful_ids = {
            body["id"] for status, body in results if status == 201
        }
        self.assertEqual(len(successful_ids), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_different_keys_same_payload_still_use_date_conflict_rule(self) -> None:
        first = self.post_leave(operation_id=str(uuid4()))
        second = self.post_leave(operation_id=str(uuid4()))
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "LEAVE_CONFLICT")
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_hr_review_processing_operation_is_visible_to_its_actor(self) -> None:
        operation_id = str(uuid4())
        self.reserve(operation_id, operation_type="HR_REVIEW")
        response = self.get_operation(operation_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["operation_type"], "HR_REVIEW")
        self.assertEqual(response.json()["status"], "PROCESSING")


if __name__ == "__main__":
    unittest.main()
