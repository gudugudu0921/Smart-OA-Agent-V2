from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.agents.chat_agent import recover_chat_confirm_operation, run_chat_agent
from app.db.redis import Redis7CheckpointSaver
from app.models.audit_log import AuditLog
from app.models.business_operation import BusinessOperation
from app.models.leave_application import LeaveApplication
from app.models.notification import Notification
from app.models.user import User
from app.schemas.leave import LeaveCreate
from app.services import leave_service
from app.services.idempotency_service import OperationResultUnknownError
from app.services.session_service import (
    ChatTurnLockLostError,
    MessageRequestResultUnknownError,
    acquire_chat_turn_lock,
    begin_message_request,
    calculate_message_request_fingerprint,
    complete_message_request,
)
from tests.integration.test_mysql_create_leave_idempotency import (
    execute_operation_migration,
)


MYSQL_ENV = "SMART_OA_CHAT_SUBMIT_TEST_DATABASE_URL"
REDIS_ENV = "SMART_OA_CHAT_TEST_REDIS_URL"
MYSQL_PREFIX = "smartoa_phase2a4c_"


@unittest.skipUnless(
    os.getenv(MYSQL_ENV) and os.getenv(REDIS_ENV),
    f"{MYSQL_ENV} and {REDIS_ENV} are required for Chat submit integration tests",
)
class MySQLRedisChatSubmitIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.environ[MYSQL_ENV]
        parsed = make_url(database_url)
        if parsed.drivername != "mysql+pymysql":
            raise RuntimeError("Chat integration URL must use mysql+pymysql")
        if parsed.host not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("Chat integration MySQL must bind to loopback")
        if not (parsed.database or "").startswith(MYSQL_PREFIX):
            raise RuntimeError("Refusing to use a non-Phase-2A-4C database")

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
            manager = User(
                id=9701,
                employee_no="CHAT9701",
                username="chat_submit_manager",
                password_hash="unused-in-isolated-tests",
                name="Chat Submit Manager",
                role="manager",
                department="Testing",
                manager_id=None,
                is_active=True,
                must_change_password=False,
            )
            employee = User(
                id=9702,
                employee_no="CHAT9702",
                username="chat_submit_employee",
                password_hash="unused-in-isolated-tests",
                name="Chat Submit Employee",
                role="employee",
                department="Testing",
                manager_id=9701,
                is_active=True,
                must_change_password=False,
            )
            for user in (manager, employee):
                if db.get(User, user.id) is None:
                    db.add(user)
            db.commit()

        cls.redis_client = Redis.from_url(
            os.environ[REDIS_ENV], decode_responses=False
        )
        cls.redis_client.ping()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.redis_client.close()
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM notifications"))
            connection.execute(text("DELETE FROM audit_logs"))
            connection.execute(text("DELETE FROM leave_applications"))
            connection.execute(text("DELETE FROM business_operations"))
        self.prefix = f"smartoa-phase2a4c-{uuid4()}"
        self.thread_id = str(uuid4())
        self.operation_id = str(uuid4())
        self.saver = Redis7CheckpointSaver(
            self.redis_client,
            key_prefix=self.prefix,
        )
        self.saver.setup()
        with self.SessionFactory() as db:
            self.employee = db.get(User, 9702)
            db.expunge(self.employee)
        self.patches = (
            patch("app.services.session_service.settings.redis_key_prefix", self.prefix),
            patch(
                "app.services.session_service.get_redis_client",
                return_value=self.redis_client,
            ),
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=self.saver,
            ),
        )
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        keys = list(self.redis_client.scan_iter(match=f"{self.prefix}:*", count=100))
        if keys:
            self.redis_client.delete(*keys)

    def counts(self) -> tuple[int, int, int]:
        with self.SessionFactory() as db:
            return (
                db.query(LeaveApplication).count(),
                db.query(AuditLog).filter(AuditLog.action == "CREATE_LEAVE").count(),
                db.query(Notification)
                .filter(Notification.type == "leave_created")
                .count(),
            )

    @staticmethod
    def submit_recognition(_state):
        return {
            "intent": "submit_leave",
            "extracted_info": {
                "leave_type": "annual",
                "start_date": date(2035, 6, 1),
                "end_date": date(2035, 6, 2),
                "reason": "Phase 2A-4C isolated integration",
            },
            "error": None,
        }

    def prepare_confirmation(self, *, expect_empty_business_state: bool = True):
        lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        try:
            result = run_chat_agent(
                self.SessionFactory(),
                self.employee,
                self.thread_id,
                "我要请年假",
                recognizer=self.submit_recognition,
                message_request_id=str(uuid4()),
                operation_id=self.operation_id,
                task_revision=0,
                turn_lock=lock,
                idempotency_session_factory=self.SessionFactory,
            )
        finally:
            lock.release()
        self.assertEqual(result["task_phase"], "AWAITING_CONFIRMATION")
        if expect_empty_business_state:
            self.assertEqual(self.counts(), (0, 0, 0))
        return result

    def confirm(self, revision: int):
        lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        try:
            return run_chat_agent(
                self.SessionFactory(),
                self.employee,
                self.thread_id,
                "确认",
                recognizer=lambda _state: self.fail("confirm must bypass recognition"),
                message_request_id=str(uuid4()),
                operation_id=self.operation_id,
                task_action="confirm",
                task_revision=revision,
                turn_lock=lock,
                idempotency_session_factory=self.SessionFactory,
            )
        finally:
            lock.release()

    def test_confirm_creates_once_and_different_message_replays(self) -> None:
        ready = self.prepare_confirmation()
        first = self.confirm(ready["task_revision"])
        replay = self.confirm(ready["task_revision"])

        self.assertTrue(first["success"])
        self.assertEqual(first["task_phase"], "SUCCEEDED")
        self.assertEqual(first["response_operation_status"], "SUCCEEDED")
        self.assertFalse(first["response_replayed"])
        self.assertTrue(replay["success"])
        self.assertTrue(replay["response_replayed"])
        self.assertEqual(
            first["tool_result"]["data"]["application_id"],
            replay["tool_result"]["data"]["application_id"],
        )
        self.assertEqual(self.counts(), (1, 1, 1))

        next_operation_id = str(uuid4())
        lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        try:
            next_task = run_chat_agent(
                self.SessionFactory(),
                self.employee,
                self.thread_id,
                "再请一笔独立年假",
                recognizer=lambda _state: {
                    "intent": "submit_leave",
                    "extracted_info": {
                        "leave_type": "annual",
                        "start_date": date(2035, 7, 1),
                        "end_date": date(2035, 7, 1),
                        "reason": "second independent task",
                    },
                    "error": None,
                },
                message_request_id=str(uuid4()),
                operation_id=next_operation_id,
                task_revision=0,
                turn_lock=lock,
                idempotency_session_factory=self.SessionFactory,
            )
        finally:
            lock.release()
        self.assertEqual(next_task["task_phase"], "AWAITING_CONFIRMATION")
        self.assertEqual(next_task["active_operation_id"], next_operation_id)
        self.assertIn(self.operation_id, next_task["closed_operation_ids"])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_lost_lock_before_executing_barrier_never_calls_mysql(self) -> None:
        ready = self.prepare_confirmation()
        lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        self.redis_client.set(lock.key, "new-owner", ex=90)
        try:
            with patch(
                "app.tools.leave_tools.create_leave_application_idempotent"
            ) as create:
                with self.assertRaises(ChatTurnLockLostError):
                    run_chat_agent(
                        self.SessionFactory(),
                        self.employee,
                        self.thread_id,
                        "确认",
                        recognizer=lambda _state: self.fail(
                            "confirm must bypass recognition"
                        ),
                        message_request_id=str(uuid4()),
                        operation_id=self.operation_id,
                        task_action="confirm",
                        task_revision=ready["task_revision"],
                        turn_lock=lock,
                        idempotency_session_factory=self.SessionFactory,
                    )
            create.assert_not_called()
            self.assertEqual(self.counts(), (0, 0, 0))
        finally:
            lock.release()

    def test_executing_checkpoint_without_mysql_record_stays_unknown(self) -> None:
        ready = self.prepare_confirmation()
        lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        try:
            with patch(
                "app.agents.chat_agent._execute_confirmed_submit_leave",
                side_effect=RuntimeError("simulated crash after barrier"),
            ):
                with self.assertRaises(RuntimeError):
                    run_chat_agent(
                        self.SessionFactory(),
                        self.employee,
                        self.thread_id,
                        "确认",
                        recognizer=lambda _state: self.fail(
                            "confirm must bypass recognition"
                        ),
                        message_request_id=str(uuid4()),
                        operation_id=self.operation_id,
                        task_action="confirm",
                        task_revision=ready["task_revision"],
                        turn_lock=lock,
                        idempotency_session_factory=self.SessionFactory,
                    )
        finally:
            lock.release()

        stored = self.saver.get_tuple(
            {"configurable": {"thread_id": self.thread_id}}
        )
        self.assertEqual(stored.checkpoint["channel_values"]["task_phase"], "EXECUTING")
        recovery_lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        try:
            recovered = recover_chat_confirm_operation(
                session_factory=self.SessionFactory,
                current_user=self.employee,
                thread_id=self.thread_id,
                operation_id=self.operation_id,
                task_revision=ready["task_revision"],
                turn_lock=recovery_lock,
            )
        finally:
            recovery_lock.release()
        self.assertEqual(recovered["task_phase"], "UNKNOWN")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_awaiting_checkpoint_reconciles_existing_mysql_success(self) -> None:
        application = leave_service.create_leave_application_idempotent(
            self.SessionFactory,
            self.employee,
            LeaveCreate(
                leave_type="annual",
                start_date=date(2035, 6, 1),
                end_date=date(2035, 6, 2),
                reason="Phase 2A-4C isolated integration",
            ),
            self.operation_id,
        )
        ready = self.prepare_confirmation(expect_empty_business_state=False)
        replay = self.confirm(ready["task_revision"])
        self.assertTrue(replay["success"])
        self.assertTrue(replay["response_replayed"])
        self.assertEqual(
            replay["tool_result"]["data"]["application_id"], application.id
        )
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_chat_snapshot_conflict_never_replays_existing_operation(self) -> None:
        leave_service.create_leave_application_idempotent(
            self.SessionFactory,
            self.employee,
            LeaveCreate(
                leave_type="annual",
                start_date=date(2035, 6, 1),
                end_date=date(2035, 6, 2),
                reason="different original payload",
            ),
            self.operation_id,
        )
        ready = self.prepare_confirmation(expect_empty_business_state=False)
        conflict = self.confirm(ready["task_revision"])
        self.assertFalse(conflict["success"])
        self.assertEqual(conflict["code"], "IDEMPOTENCY_CONFLICT")
        self.assertIsNone(conflict["response_operation_status"])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_succeeded_replay_rejects_mismatched_application_owner(self) -> None:
        ready = self.prepare_confirmation()
        first = self.confirm(ready["task_revision"])
        application_id = first["tool_result"]["data"]["application_id"]
        with self.SessionFactory() as db:
            application = db.get(LeaveApplication, application_id)
            application.employee_id = 9701
            db.commit()

        recovered = self.confirm(ready["task_revision"])
        self.assertFalse(recovered["success"])
        self.assertEqual(recovered["task_phase"], "UNKNOWN")
        self.assertEqual(recovered["response_operation_status"], "UNKNOWN")
        self.assertIsNone(recovered.get("tool_result"))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_mysql_success_then_checkpoint_loss_recovers_without_mutation(self) -> None:
        ready = self.prepare_confirmation()
        lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        real_create = leave_service.create_leave_application_idempotent

        def commit_then_steal(**kwargs):
            application = real_create(**kwargs)
            self.redis_client.set(lock.key, "new-owner", ex=90)
            return application

        try:
            with patch(
                "app.tools.leave_tools.create_leave_application_idempotent",
                side_effect=commit_then_steal,
            ):
                with self.assertRaises(ChatTurnLockLostError):
                    run_chat_agent(
                        self.SessionFactory(),
                        self.employee,
                        self.thread_id,
                        "确认",
                        recognizer=lambda _state: self.fail(
                            "confirm must bypass recognition"
                        ),
                        message_request_id=str(uuid4()),
                        operation_id=self.operation_id,
                        task_action="confirm",
                        task_revision=ready["task_revision"],
                        turn_lock=lock,
                        idempotency_session_factory=self.SessionFactory,
                    )
        finally:
            lock.release()

        self.assertEqual(self.counts(), (1, 1, 1))
        self.redis_client.delete(lock.key)
        recovery_lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        try:
            recovered = recover_chat_confirm_operation(
                session_factory=self.SessionFactory,
                current_user=self.employee,
                thread_id=self.thread_id,
                operation_id=self.operation_id,
                task_revision=ready["task_revision"],
                turn_lock=recovery_lock,
            )
        finally:
            recovery_lock.release()
        self.assertTrue(recovered["success"])
        self.assertTrue(recovered["response_replayed"])
        self.assertEqual(recovered["task_phase"], "SUCCEEDED")
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_processing_receipt_recovers_succeeded_mysql_result(self) -> None:
        ready = self.prepare_confirmation()
        request_id = str(uuid4())
        fingerprint = calculate_message_request_fingerprint(
            message="确认",
            operation_id=self.operation_id,
            task_action="confirm",
            task_revision=ready["task_revision"],
        )
        begin_message_request(
            self.employee.id,
            self.thread_id,
            request_id,
            fingerprint=fingerprint,
        )
        first = self.confirm(ready["task_revision"])
        with self.assertRaises(MessageRequestResultUnknownError):
            begin_message_request(
                self.employee.id,
                self.thread_id,
                request_id,
                fingerprint=fingerprint,
            )

        lock = acquire_chat_turn_lock(self.employee.id, self.thread_id)
        try:
            recovered = recover_chat_confirm_operation(
                session_factory=self.SessionFactory,
                current_user=self.employee,
                thread_id=self.thread_id,
                operation_id=self.operation_id,
                task_revision=ready["task_revision"],
                turn_lock=lock,
            )
        finally:
            lock.release()
        complete_message_request(
            self.employee.id,
            self.thread_id,
            request_id,
            fingerprint=fingerprint,
            response={
                "success": recovered["success"],
                "application_id": recovered["tool_result"]["data"][
                    "application_id"
                ],
            },
        )
        receipt = begin_message_request(
            self.employee.id,
            self.thread_id,
            request_id,
            fingerprint=fingerprint,
        )
        self.assertEqual(receipt.disposition, "replay")
        self.assertEqual(
            receipt.response["application_id"],
            first["tool_result"]["data"]["application_id"],
        )
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_different_authenticated_user_cannot_recover_operation(self) -> None:
        ready = self.prepare_confirmation()
        self.confirm(ready["task_revision"])
        with self.SessionFactory() as db:
            other_user = db.get(User, 9701)
            db.expunge(other_user)
        lock = acquire_chat_turn_lock(other_user.id, self.thread_id)
        try:
            with self.assertRaises(OperationResultUnknownError):
                recover_chat_confirm_operation(
                    session_factory=self.SessionFactory,
                    current_user=other_user,
                    thread_id=self.thread_id,
                    operation_id=self.operation_id,
                    task_revision=ready["task_revision"],
                    turn_lock=lock,
                )
        finally:
            lock.release()
        self.assertEqual(self.counts(), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
