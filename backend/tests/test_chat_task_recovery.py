from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.chat_agent import (
    _confirmation_summary,
    project_chat_task_state,
    run_chat_agent,
)
from app.api.routes.chat import chat as chat_route
from app.api.routes.chat import get_chat_session_task
from app.core.dependencies import get_current_business_user
from app.db.mysql import get_db
from app.db.redis import RedisUnavailableError
from app.main import app
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.idempotency_service import calculate_request_fingerprint
from app.services.session_service import (
    MessageRequestReceipt,
    SessionNotFoundError,
)


def recovery_user(user_id: int = 9801) -> User:
    return User(
        id=user_id,
        employee_no=f"RECOVERY{user_id}",
        username=f"recovery_user_{user_id}",
        password_hash="unused-in-isolated-tests",
        name="Recovery User",
        role="employee",
        department="Testing",
        manager_id=9802,
        is_active=True,
        must_change_password=False,
    )


def ready_state(
    *,
    user_id: int,
    thread_id: str,
    operation_id: str,
    revision: int = 2,
    reason: str | None = "家庭事务",
) -> dict:
    start = date.today() + timedelta(days=20)
    snapshot = {
        "leave_type": "annual",
        "start_date": start.isoformat(),
        "end_date": start.isoformat(),
        "reason": reason,
    }
    fingerprint = calculate_request_fingerprint("SUBMIT_LEAVE", snapshot)
    return {
        "active_operation_id": operation_id,
        "active_operation_type": "SUBMIT_LEAVE",
        "active_task_user_id": user_id,
        "active_task_thread_id": thread_id,
        "task_phase": "AWAITING_CONFIRMATION",
        "task_revision": revision,
        "task_payload_snapshot": snapshot,
        "task_fingerprint": fingerprint,
        "confirmation_fingerprint": fingerprint,
        "pending_intent": "submit_leave",
    }


class _TestTurnLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def ensure_owned(self) -> None:
        return None


class ConfirmationProjectionTests(unittest.TestCase):
    def test_confirmation_summary_is_a_valid_string(self) -> None:
        snapshot = ready_state(
            user_id=1,
            thread_id=str(uuid4()),
            operation_id=str(uuid4()),
        )["task_payload_snapshot"]

        summary = _confirmation_summary(snapshot, 3)

        self.assertIsInstance(summary, str)
        self.assertIn("版本 3", summary)
        self.assertIn(snapshot["start_date"], summary)
        self.assertIn("家庭事务", summary)

    def test_confirmation_summary_rejects_invalid_snapshot(self) -> None:
        with self.assertRaises(ValueError):
            _confirmation_summary({"leave_type": "annual"}, 0)

    def test_awaiting_projection_matches_validated_snapshot(self) -> None:
        thread_id = str(uuid4())
        operation_id = str(uuid4())
        state = ready_state(
            user_id=10,
            thread_id=thread_id,
            operation_id=operation_id,
        )

        result = project_chat_task_state(state, user_id=10, thread_id=thread_id)

        self.assertEqual(result["operation_id"], operation_id)
        self.assertEqual(result["task_status"], "awaiting_confirmation")
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(
            result["confirmation"],
            {**state["task_payload_snapshot"], "task_revision": 2},
        )
        self.assertIsNone(result["operation_status"])

    def test_revision_change_updates_confirmation(self) -> None:
        thread_id = str(uuid4())
        operation_id = str(uuid4())
        before = ready_state(
            user_id=10,
            thread_id=thread_id,
            operation_id=operation_id,
            revision=1,
            reason="原原因",
        )
        after = ready_state(
            user_id=10,
            thread_id=thread_id,
            operation_id=operation_id,
            revision=2,
            reason="新原因",
        )

        old_view = project_chat_task_state(before, user_id=10, thread_id=thread_id)
        new_view = project_chat_task_state(after, user_id=10, thread_id=thread_id)

        self.assertEqual(old_view["confirmation"]["task_revision"], 1)
        self.assertEqual(new_view["confirmation"]["task_revision"], 2)
        self.assertEqual(new_view["confirmation"]["reason"], "新原因")

    def test_null_reason_is_preserved_in_confirmation(self) -> None:
        thread_id = str(uuid4())
        state = ready_state(
            user_id=10,
            thread_id=thread_id,
            operation_id=str(uuid4()),
            reason=None,
        )

        result = project_chat_task_state(state, user_id=10, thread_id=thread_id)

        self.assertTrue(result["requires_confirmation"])
        self.assertIsNone(result["confirmation"]["reason"])

    def test_non_confirmable_states_never_expose_confirmation(self) -> None:
        thread_id = str(uuid4())
        operation_id = str(uuid4())
        base = ready_state(
            user_id=10,
            thread_id=thread_id,
            operation_id=operation_id,
        )
        for phase in ("COLLECTING", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN"):
            with self.subTest(phase=phase):
                state = {**base, "task_phase": phase}
                result = project_chat_task_state(
                    state, user_id=10, thread_id=thread_id
                )
                self.assertFalse(result["requires_confirmation"])
                self.assertIsNone(result["confirmation"])
                self.assertIsNone(result["operation_status"])

    def test_idle_cancelled_and_old_checkpoint_are_conservative(self) -> None:
        thread_id = str(uuid4())
        idle = project_chat_task_state({}, user_id=10, thread_id=thread_id)
        legacy = project_chat_task_state(
            {
                "pending_intent": "submit_leave",
                "pending_fields": [],
                "extracted_info": {
                    "leave_type": "annual",
                    "start_date": "2030-01-01",
                    "end_date": "2030-01-01",
                },
            },
            user_id=10,
            thread_id=thread_id,
        )

        self.assertEqual(idle["task_status"], "idle")
        self.assertIsNone(idle["operation_id"])
        self.assertEqual(legacy["task_status"], "collecting")
        self.assertEqual(legacy["code"], "CHAT_TASK_PROTOCOL_UPGRADE_REQUIRED")
        self.assertFalse(legacy["requires_confirmation"])
        self.assertIsNone(legacy["confirmation"])

    def test_invalid_fingerprint_or_owner_never_exposes_confirmation(self) -> None:
        thread_id = str(uuid4())
        state = ready_state(
            user_id=10,
            thread_id=thread_id,
            operation_id=str(uuid4()),
        )
        for changes in (
            {"confirmation_fingerprint": "0" * 64},
            {"active_task_user_id": 11},
            {"active_task_thread_id": str(uuid4())},
        ):
            with self.subTest(changes=changes):
                result = project_chat_task_state(
                    {**state, **changes}, user_id=10, thread_id=thread_id
                )
                self.assertEqual(result["task_status"], "unknown")
                self.assertFalse(result["requires_confirmation"])
                self.assertIsNone(result["confirmation"])


class ChatTaskRecoveryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = recovery_user()

    def test_complete_chat_api_serializes_structured_confirmation(self) -> None:
        thread_id = str(uuid4())
        operation_id = str(uuid4())
        request_id = str(uuid4())
        start = date.today() + timedelta(days=30)
        saver = InMemorySaver()
        actual_run = run_chat_agent

        def run_actual_graph(**kwargs):
            kwargs["turn_lock"] = None
            kwargs["idempotency_session_factory"] = None
            kwargs["recognizer"] = lambda _state: {
                "intent": "submit_leave",
                "extracted_info": {
                    "leave_type": "annual",
                    "start_date": start,
                    "end_date": start,
                    "reason": "结构化确认测试",
                },
                "error": None,
            }
            return actual_run(**kwargs)

        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch(
                "app.api.routes.chat.acquire_chat_turn_lock",
                return_value=_TestTurnLock(),
            ),
            patch(
                "app.api.routes.chat.begin_message_request",
                return_value=MessageRequestReceipt("new"),
            ),
            patch("app.api.routes.chat.complete_message_request"),
            patch("app.api.routes.chat.touch_session"),
            patch("app.api.routes.chat.run_chat_agent", side_effect=run_actual_graph),
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=saver,
            ),
        ):
            response = chat_route(
                payload=ChatRequest.model_validate(
                    {
                        "thread_id": thread_id,
                        "message": "我要请年假",
                        "message_request_id": request_id,
                        "operation_id": operation_id,
                        "task_action": "message",
                        "task_revision": 0,
                    }
                ),
                request=MagicMock(client=MagicMock(host="127.0.0.1")),
                db=MagicMock(),
                current_user=self.user,
                idempotency_session_factory=MagicMock(),
            )

        body = response.model_dump(mode="json")
        self.assertEqual(body["task_status"], "awaiting_confirmation")
        self.assertTrue(body["requires_confirmation"])
        self.assertIsInstance(body["response"], str)
        self.assertEqual(
            body["confirmation"],
            {
                "leave_type": "annual",
                "start_date": start.isoformat(),
                "end_date": start.isoformat(),
                "reason": "结构化确认测试",
                "task_revision": 0,
            },
        )

    def test_legacy_chat_response_omits_confirmation(self) -> None:
        response = ChatResponse(
            success=True,
            thread_id=uuid4(),
            intent="general_chat",
            response="你好",
            data=None,
        ).model_dump(mode="json")
        self.assertNotIn("confirmation", response)

    def test_owner_can_read_awaiting_task_without_running_graph(self) -> None:
        thread_id = str(uuid4())
        operation_id = str(uuid4())
        state = ready_state(
            user_id=self.user.id,
            thread_id=thread_id,
            operation_id=operation_id,
        )
        with (
            patch(
                "app.api.routes.chat.get_session_task_checkpoint",
                return_value=state,
            ) as read_checkpoint,
            patch("app.api.routes.chat.run_chat_agent") as run_graph,
        ):
            response = get_chat_session_task(
                thread_id=UUID(thread_id),
                current_user=self.user,
            )

        body = response.model_dump(mode="json")
        self.assertEqual(body["operation_id"], operation_id)
        self.assertTrue(body["requires_confirmation"])
        read_checkpoint.assert_called_once_with(self.user.id, UUID(thread_id))
        run_graph.assert_not_called()


class ChatTaskRecoveryHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.user = recovery_user(9811)

        def override_get_db():
            db = MagicMock()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_business_user] = lambda: cls.user
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        app.dependency_overrides.clear()

    def test_http_chat_returns_renderable_confirmation(self) -> None:
        thread_id = str(uuid4())
        operation_id = str(uuid4())
        start = date.today() + timedelta(days=40)
        saver = InMemorySaver()
        actual_run = run_chat_agent

        def run_actual_graph(**kwargs):
            kwargs["turn_lock"] = None
            kwargs["idempotency_session_factory"] = None
            kwargs["recognizer"] = lambda _state: {
                "intent": "submit_leave",
                "extracted_info": {
                    "leave_type": "personal",
                    "start_date": start,
                    "end_date": start,
                    "reason": "HTTP 确认测试",
                },
                "error": None,
            }
            return actual_run(**kwargs)

        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch(
                "app.api.routes.chat.acquire_chat_turn_lock",
                return_value=_TestTurnLock(),
            ),
            patch(
                "app.api.routes.chat.begin_message_request",
                return_value=MessageRequestReceipt("new"),
            ),
            patch("app.api.routes.chat.complete_message_request"),
            patch("app.api.routes.chat.touch_session"),
            patch("app.api.routes.chat.run_chat_agent", side_effect=run_actual_graph),
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=saver,
            ),
        ):
            response = self.client.post(
                "/chat",
                json={
                    "thread_id": thread_id,
                    "message": "我要请事假",
                    "message_request_id": str(uuid4()),
                    "operation_id": operation_id,
                    "task_action": "message",
                    "task_revision": 0,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["task_status"], "awaiting_confirmation")
        self.assertEqual(body["operation_id"], operation_id)
        self.assertEqual(body["confirmation"]["leave_type"], "personal")
        self.assertEqual(body["confirmation"]["start_date"], start.isoformat())
        self.assertEqual(body["confirmation"]["task_revision"], 0)

    def test_http_task_recovery_is_owned_and_read_only(self) -> None:
        thread_id = str(uuid4())
        operation_id = str(uuid4())
        state = ready_state(
            user_id=self.user.id,
            thread_id=thread_id,
            operation_id=operation_id,
        )
        with (
            patch(
                "app.api.routes.chat.get_session_task_checkpoint",
                return_value=state,
            ),
            patch("app.api.routes.chat.run_chat_agent") as run_graph,
        ):
            response = self.client.get(f"/chat/sessions/{thread_id}/task")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["operation_id"], operation_id)
        self.assertTrue(response.json()["requires_confirmation"])
        run_graph.assert_not_called()

        with patch(
            "app.api.routes.chat.get_session_task_checkpoint",
            side_effect=SessionNotFoundError("会话不存在。"),
        ):
            hidden = self.client.get(f"/chat/sessions/{thread_id}/task")
        self.assertEqual(hidden.status_code, 404, hidden.text)
        self.assertEqual(hidden.json()["code"], "SESSION_NOT_FOUND")

    def test_http_task_recovery_redis_failure_is_safe_503(self) -> None:
        thread_id = str(uuid4())
        with patch(
            "app.api.routes.chat.get_session_task_checkpoint",
            side_effect=RedisUnavailableError("redis://private-host"),
        ):
            response = self.client.get(f"/chat/sessions/{thread_id}/task")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["code"], "REDIS_UNAVAILABLE")
        self.assertNotIn("private-host", response.text)

    def test_other_user_cannot_read_task(self) -> None:
        thread_id = str(uuid4())
        with (
            patch(
                "app.api.routes.chat.get_session_task_checkpoint",
                side_effect=SessionNotFoundError("会话不存在。"),
            ),
            self.assertRaises(SessionNotFoundError),
        ):
            get_chat_session_task(
                thread_id=UUID(thread_id),
                current_user=self.user,
            )

    def test_redis_failure_is_safe_503(self) -> None:
        thread_id = str(uuid4())
        with (
            patch(
                "app.api.routes.chat.get_session_task_checkpoint",
                side_effect=RedisUnavailableError("redis://private-host"),
            ),
            patch("app.api.routes.chat.run_chat_agent") as run_graph,
        ):
            with self.assertRaises(RedisUnavailableError):
                get_chat_session_task(
                    thread_id=UUID(thread_id),
                    current_user=self.user,
                )
        run_graph.assert_not_called()


if __name__ == "__main__":
    unittest.main()
