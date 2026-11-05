from __future__ import annotations

from threading import Lock
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from redis.exceptions import RedisError

from app.api.routes.chat import chat
from app.core.exceptions import AppException
from app.db.redis import RedisUnavailableError
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.session_service import (
    ChatTurnInProgressError,
    MessageRequestConflictError,
    MessageRequestReceipt,
    MessageRequestResultUnknownError,
    acquire_chat_turn_lock,
    begin_message_request,
    calculate_message_request_fingerprint,
    complete_message_request,
)


class _MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.mutex = Lock()
        self.fail = False

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise RedisError("private redis address")
        encoded = value.encode() if isinstance(value, str) else value
        with self.mutex:
            if nx and key in self.values:
                return None
            self.values[key] = encoded
        return True

    def get(self, key):
        if self.fail:
            raise RedisError("private redis address")
        with self.mutex:
            return self.values.get(key)

    def eval(self, script, _keys, key, owner, *args):
        if self.fail:
            raise RedisError("private redis address")
        with self.mutex:
            current = self.values.get(key)
            owner_bytes = owner.encode()
            if current != owner_bytes:
                return 0
            if "expire" in script:
                return 1
            del self.values[key]
            return 1


def _user() -> User:
    return User(
        id=8901,
        employee_no="SAFE8901",
        username="chat_safety_user",
        password_hash="unused-in-isolated-tests",
        name="Chat Safety User",
        role="employee",
        department="Testing",
        manager_id=8902,
        is_active=True,
        must_change_password=False,
    )


class ChatTurnLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = _MemoryRedis()
        self.client_patch = patch(
            "app.services.session_service.get_redis_client",
            return_value=self.redis,
        )
        self.client_patch.start()

    def tearDown(self) -> None:
        self.client_patch.stop()

    def test_same_user_and_thread_are_serialized(self) -> None:
        first = acquire_chat_turn_lock(1, "thread-a")
        try:
            with self.assertRaises(ChatTurnInProgressError):
                acquire_chat_turn_lock(1, "thread-a")
        finally:
            first.release()

        second = acquire_chat_turn_lock(1, "thread-a")
        second.release()

    def test_old_owner_cannot_release_a_new_owners_lock(self) -> None:
        first = acquire_chat_turn_lock(2, "thread-b")
        key = first.key
        self.redis.values[key] = b"new-owner"
        first.release()
        self.assertEqual(self.redis.values[key], b"new-owner")

    def test_redis_unavailable_keeps_safe_error_mapping(self) -> None:
        self.redis.fail = True
        with self.assertRaises(RedisUnavailableError) as raised:
            acquire_chat_turn_lock(3, "thread-c")
        self.assertNotIn("private redis address", str(raised.exception))


class MessageReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = _MemoryRedis()
        self.client_patch = patch(
            "app.services.session_service.get_redis_client",
            return_value=self.redis,
        )
        self.client_patch.start()
        self.request_id = str(uuid4())
        self.fingerprint = calculate_message_request_fingerprint(
            message="测试消息",
            operation_id=None,
            task_action="message",
            task_revision=None,
        )

    def tearDown(self) -> None:
        self.client_patch.stop()

    def test_completed_receipt_replays_without_reprocessing(self) -> None:
        first = begin_message_request(
            1, "thread", self.request_id, fingerprint=self.fingerprint
        )
        self.assertEqual(first.disposition, "new")
        response = {
            "success": True,
            "thread_id": str(uuid4()),
            "intent": "general_chat",
            "response": "完成",
            "data": None,
        }
        complete_message_request(
            1,
            "thread",
            self.request_id,
            fingerprint=self.fingerprint,
            response=response,
        )
        replay = begin_message_request(
            1, "thread", self.request_id, fingerprint=self.fingerprint
        )
        self.assertEqual(replay.disposition, "replay")
        self.assertEqual(replay.response, response)

    def test_same_id_with_different_payload_is_rejected(self) -> None:
        begin_message_request(
            1, "thread", self.request_id, fingerprint=self.fingerprint
        )
        with self.assertRaises(MessageRequestConflictError):
            begin_message_request(
                1, "thread", self.request_id, fingerprint="0" * 64
            )

    def test_unfinished_receipt_is_unknown_and_not_reexecuted(self) -> None:
        begin_message_request(
            1, "thread", self.request_id, fingerprint=self.fingerprint
        )
        with self.assertRaises(MessageRequestResultUnknownError):
            begin_message_request(
                1, "thread", self.request_id, fingerprint=self.fingerprint
            )

    def test_request_fingerprint_binds_all_protocol_inputs(self) -> None:
        baseline = self.fingerprint
        variants = [
            calculate_message_request_fingerprint(
                message="另一条消息",
                operation_id=None,
                task_action="message",
                task_revision=None,
            ),
            calculate_message_request_fingerprint(
                message="测试消息",
                operation_id=uuid4(),
                task_action="message",
                task_revision=0,
            ),
            calculate_message_request_fingerprint(
                message="测试消息",
                operation_id=None,
                task_action="cancel",
                task_revision=None,
            ),
        ]
        self.assertTrue(all(item != baseline for item in variants))

    def test_same_request_id_with_different_candidate_operation_conflicts(self) -> None:
        first = calculate_message_request_fingerprint(
            message="查询我的请假记录",
            operation_id=uuid4(),
            task_action="message",
            task_revision=0,
        )
        second = calculate_message_request_fingerprint(
            message="查询我的请假记录",
            operation_id=uuid4(),
            task_action="message",
            task_revision=0,
        )
        begin_message_request(1, "thread", self.request_id, fingerprint=first)

        with self.assertRaises(MessageRequestConflictError):
            begin_message_request(
                1,
                "thread",
                self.request_id,
                fingerprint=second,
            )


class ChatReplayRouteTests(unittest.TestCase):
    def test_completed_receipt_returns_replay_without_graph_or_history_append(self) -> None:
        thread_id = uuid4()
        request_id = uuid4()
        payload = ChatRequest(
            thread_id=thread_id,
            message="你好",
            message_request_id=request_id,
        )
        stored = ChatResponse(
            success=True,
            thread_id=thread_id,
            intent="general_chat",
            response="已处理",
            data=None,
            message_request_id=request_id,
            replayed=False,
        ).model_dump(mode="json")
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = None
        request = MagicMock()
        request.client.host = "127.0.0.1"

        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch("app.api.routes.chat.acquire_chat_turn_lock", return_value=lock),
            patch(
                "app.api.routes.chat.begin_message_request",
                return_value=MessageRequestReceipt("replay", stored),
            ),
            patch("app.api.routes.chat.run_chat_agent") as run_agent,
            patch("app.api.routes.chat.touch_session") as touch_session,
        ):
            response = chat(payload, request, MagicMock(), _user())

        self.assertTrue(response.replayed)
        self.assertEqual(response.response, "已处理")
        run_agent.assert_not_called()
        touch_session.assert_not_called()

    def test_completed_candidate_receipt_replays_without_binding_operation(self) -> None:
        thread_id = uuid4()
        request_id = uuid4()
        candidate_id = uuid4()
        payload = ChatRequest(
            thread_id=thread_id,
            message="你好",
            message_request_id=request_id,
            operation_id=candidate_id,
            task_revision=0,
        )
        stored = ChatResponse(
            success=True,
            thread_id=thread_id,
            intent="general_chat",
            response="你好，我是智能 OA 请假助手。",
            data=None,
            message_request_id=request_id,
            replayed=False,
        ).model_dump(mode="json")
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = None
        request = MagicMock()
        request.client.host = "127.0.0.1"

        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch("app.api.routes.chat.acquire_chat_turn_lock", return_value=lock),
            patch(
                "app.api.routes.chat.begin_message_request",
                return_value=MessageRequestReceipt("replay", stored),
            ),
            patch("app.api.routes.chat.run_chat_agent") as run_agent,
            patch("app.api.routes.chat.touch_session") as touch_session,
        ):
            response = chat(payload, request, MagicMock(), _user())

        self.assertTrue(response.replayed)
        self.assertIsNone(response.operation_id)
        run_agent.assert_not_called()
        touch_session.assert_not_called()

    def test_unfinished_candidate_receipt_is_not_reexecuted(self) -> None:
        thread_id = uuid4()
        payload = ChatRequest(
            thread_id=thread_id,
            message="查询我的请假记录",
            message_request_id=uuid4(),
            operation_id=uuid4(),
            task_revision=0,
        )
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = None
        request = MagicMock()
        request.client.host = "127.0.0.1"

        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch("app.api.routes.chat.acquire_chat_turn_lock", return_value=lock),
            patch(
                "app.api.routes.chat.begin_message_request",
                side_effect=MessageRequestResultUnknownError("processing"),
            ),
            patch("app.api.routes.chat.run_chat_agent") as run_agent,
            patch("app.api.routes.chat.touch_session") as touch_session,
        ):
            with self.assertRaises(AppException) as raised:
                chat(payload, request, MagicMock(), _user())

        self.assertEqual(raised.exception.code, "CHAT_MESSAGE_RESULT_UNKNOWN")
        run_agent.assert_not_called()
        touch_session.assert_not_called()

    def test_processing_confirm_receipt_recovers_without_running_graph(self) -> None:
        thread_id = uuid4()
        request_id = uuid4()
        operation_id = uuid4()
        payload = ChatRequest(
            thread_id=thread_id,
            message="确认",
            message_request_id=request_id,
            operation_id=operation_id,
            task_action="confirm",
            task_revision=2,
        )
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = None
        recovered = {
            "success": True,
            "intent": "submit_leave",
            "response": "申请已创建",
            "tool_result": {
                "success": True,
                "data": {"application_id": str(uuid4())},
            },
            "response_operation_id": str(operation_id),
            "response_task_status": "succeeded",
            "response_task_revision": 2,
            "response_operation_status": "SUCCEEDED",
            "response_replayed": True,
            "code": "CHAT_OPERATION_SUCCEEDED",
        }
        request = MagicMock()
        request.client.host = "127.0.0.1"
        session_factory = MagicMock()
        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch("app.api.routes.chat.acquire_chat_turn_lock", return_value=lock),
            patch(
                "app.api.routes.chat.begin_message_request",
                side_effect=MessageRequestResultUnknownError("processing"),
            ),
            patch(
                "app.api.routes.chat.recover_chat_confirm_operation",
                return_value=recovered,
            ) as recover,
            patch("app.api.routes.chat.complete_message_request") as complete,
            patch("app.api.routes.chat.run_chat_agent") as run_agent,
            patch("app.api.routes.chat.touch_session") as touch_session,
        ):
            response = chat(
                payload,
                request,
                MagicMock(),
                _user(),
                session_factory,
            )

        self.assertTrue(response.success)
        self.assertTrue(response.replayed)
        self.assertEqual(response.operation_status, "SUCCEEDED")
        recover.assert_called_once()
        complete.assert_called_once()
        run_agent.assert_not_called()
        touch_session.assert_not_called()

    def test_touch_failure_after_mysql_success_leaves_receipt_uncompleted(self) -> None:
        thread_id = uuid4()
        request_id = uuid4()
        operation_id = uuid4()
        payload = ChatRequest(
            thread_id=thread_id,
            message="确认",
            message_request_id=request_id,
            operation_id=operation_id,
            task_action="confirm",
            task_revision=1,
        )
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = None
        request = MagicMock()
        request.client.host = "127.0.0.1"
        result = {
            "success": True,
            "intent": "submit_leave",
            "response": "申请已创建",
            "tool_result": {
                "success": True,
                "data": {"application_id": str(uuid4())},
            },
            "response_operation_id": str(operation_id),
            "response_task_status": "succeeded",
            "response_task_revision": 1,
            "response_operation_status": "SUCCEEDED",
            "code": "CHAT_OPERATION_SUCCEEDED",
        }
        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch("app.api.routes.chat.acquire_chat_turn_lock", return_value=lock),
            patch(
                "app.api.routes.chat.begin_message_request",
                return_value=MessageRequestReceipt("new"),
            ),
            patch("app.api.routes.chat.run_chat_agent", return_value=result),
            patch(
                "app.api.routes.chat.touch_session",
                side_effect=RedisUnavailableError("safe unavailable"),
            ),
            patch("app.api.routes.chat.complete_message_request") as complete,
        ):
            with self.assertRaises(RedisUnavailableError):
                chat(
                    payload,
                    request,
                    MagicMock(),
                    _user(),
                    MagicMock(),
                )
        complete.assert_not_called()

    def test_unknown_confirm_recovery_keeps_receipt_open_for_later_mysql_check(self) -> None:
        thread_id = uuid4()
        request_id = uuid4()
        operation_id = uuid4()
        payload = ChatRequest(
            thread_id=thread_id,
            message="确认",
            message_request_id=request_id,
            operation_id=operation_id,
            task_action="confirm",
            task_revision=1,
        )
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = None
        request = MagicMock()
        request.client.host = "127.0.0.1"
        recovered = {
            "success": False,
            "intent": "submit_leave",
            "response": "结果未知",
            "tool_result": None,
            "response_operation_id": str(operation_id),
            "response_task_status": "unknown",
            "response_task_revision": 1,
            "response_operation_status": "UNKNOWN",
            "response_replayed": True,
            "code": "OPERATION_RESULT_UNKNOWN",
        }
        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch("app.api.routes.chat.acquire_chat_turn_lock", return_value=lock),
            patch(
                "app.api.routes.chat.begin_message_request",
                side_effect=MessageRequestResultUnknownError("processing"),
            ),
            patch(
                "app.api.routes.chat.recover_chat_confirm_operation",
                return_value=recovered,
            ),
            patch("app.api.routes.chat.complete_message_request") as complete,
        ):
            response = chat(
                payload,
                request,
                MagicMock(),
                _user(),
                MagicMock(),
            )
        self.assertEqual(response.operation_status, "UNKNOWN")
        complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
