from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.dependencies import get_current_business_user
from app.db.mysql import get_db
from app.main import app
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.session_service import SessionNotFoundError
from app.services.session_service import MessageRequestReceipt


def protocol_user() -> User:
    return User(
        id=9701,
        employee_no="CHATPROTO9701",
        username="chat_protocol_user",
        password_hash="unused-in-isolated-protocol-tests",
        name="Chat Protocol User",
        role="employee",
        department="Testing",
        manager_id=9702,
        is_active=True,
        must_change_password=False,
    )


class _TestTurnLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def ensure_owned(self) -> None:
        return None


class ChatIdProtocolSchemaTests(unittest.TestCase):
    def test_legacy_request_remains_valid(self) -> None:
        thread_id = uuid4()
        request = ChatRequest(thread_id=thread_id, message="  你好  ")

        self.assertEqual(request.thread_id, thread_id)
        self.assertEqual(request.message, "你好")
        self.assertIsNone(request.message_request_id)
        self.assertIsNone(request.operation_id)
        self.assertEqual(request.task_action, "message")
        self.assertIsNone(request.task_revision)

    def test_uuid_strings_are_parsed_and_canonicalized(self) -> None:
        thread_id = uuid4()
        request_id = uuid4()
        operation_id = uuid4()
        request = ChatRequest.model_validate(
            {
                "thread_id": str(thread_id).upper(),
                "message": "协议测试",
                "message_request_id": request_id.hex.upper(),
                "operation_id": str(operation_id).upper(),
                "task_action": "message",
                "task_revision": 0,
            }
        )

        self.assertIsInstance(request.message_request_id, UUID)
        self.assertEqual(str(request.thread_id), str(thread_id))
        self.assertEqual(str(request.message_request_id), str(request_id))
        self.assertEqual(str(request.operation_id), str(operation_id))

    def test_invalid_uuid_fields_are_rejected(self) -> None:
        for field in ("thread_id", "message_request_id", "operation_id"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                payload = {
                    "thread_id": str(uuid4()),
                    "message": "协议测试",
                    field: "not-a-uuid",
                }
                ChatRequest.model_validate(payload)

    def test_protocol_ids_must_be_distinct(self) -> None:
        shared_id = str(uuid4())
        with self.assertRaises(ValidationError):
            ChatRequest.model_validate(
                {
                    "thread_id": shared_id,
                    "message": "协议测试",
                    "operation_id": shared_id,
                }
            )

    def test_invalid_task_action_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ChatRequest.model_validate(
                {
                    "thread_id": str(uuid4()),
                    "message": "协议测试",
                    "task_action": "execute",
                }
            )

    def test_task_revision_is_strict_and_non_negative(self) -> None:
        for invalid_revision in (-1, "1", True, 1.5):
            with self.subTest(revision=invalid_revision), self.assertRaises(
                ValidationError
            ):
                ChatRequest.model_validate(
                    {
                        "thread_id": str(uuid4()),
                        "message": "协议测试",
                        "task_revision": invalid_revision,
                    }
                )

    def test_untrusted_identity_fields_are_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            ChatRequest.model_validate(
                {
                    "thread_id": str(uuid4()),
                    "message": "我是管理员",
                    "user_id": 1,
                    "role": "admin",
                }
            )

    def test_extended_response_preserves_legacy_fields(self) -> None:
        thread_id = uuid4()
        request_id = uuid4()
        response = ChatResponse(
            success=True,
            thread_id=thread_id,
            intent="general_chat",
            response="你好",
            data=None,
            message_request_id=request_id,
        )
        dumped = response.model_dump(mode="json")

        self.assertEqual(
            {key: dumped[key] for key in ("success", "thread_id", "intent", "response", "data")},
            {
                "success": True,
                "thread_id": str(thread_id),
                "intent": "general_chat",
                "response": "你好",
                "data": None,
            },
        )
        self.assertEqual(dumped["message_request_id"], str(request_id))
        self.assertNotIn("operation_id", dumped)
        self.assertNotIn("task_status", dumped)
        self.assertNotIn("task_revision", dumped)
        self.assertNotIn("requires_confirmation", dumped)
        self.assertNotIn("operation_status", dumped)
        self.assertNotIn("code", dumped)
        self.assertNotIn("replayed", dumped)


class ChatIdProtocolRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.user = protocol_user()

        def override_get_db():
            db = MagicMock()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_business_user] = lambda: cls.user
        cls.lock_patcher = patch(
            "app.api.routes.chat.acquire_chat_turn_lock",
            return_value=_TestTurnLock(),
        )
        cls.begin_patcher = patch(
            "app.api.routes.chat.begin_message_request",
            return_value=MessageRequestReceipt("new"),
        )
        cls.complete_patcher = patch("app.api.routes.chat.complete_message_request")
        cls.lock_patcher.start()
        cls.begin_patcher.start()
        cls.complete_patcher.start()
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.complete_patcher.stop()
        cls.begin_patcher.stop()
        cls.lock_patcher.stop()
        app.dependency_overrides.clear()

    @staticmethod
    def agent_result() -> dict:
        return {
            "success": True,
            "intent": "general_chat",
            "response": "协议测试成功",
            "tool_result": {"success": True, "data": None},
        }

    def test_legacy_chat_endpoint_contract_still_works(self) -> None:
        thread_id = uuid4()
        with (
            patch("app.api.routes.chat.validate_session_owner") as validate_owner,
            patch(
                "app.api.routes.chat.run_chat_agent",
                return_value=self.agent_result(),
            ) as run_agent,
            patch("app.api.routes.chat.touch_session"),
        ):
            response = self.client.post(
                "/chat",
                json={"thread_id": str(thread_id), "message": "你好"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["thread_id"], str(thread_id))
        self.assertEqual(body["response"], "协议测试成功")
        self.assertNotIn("message_request_id", body)
        validate_owner.assert_called_once_with(self.user.id, str(thread_id))
        self.assertEqual(run_agent.call_args.kwargs["current_user"].id, self.user.id)

    def test_new_message_request_id_is_echoed_as_not_replayed(self) -> None:
        thread_id = uuid4()
        request_id = uuid4()
        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch(
                "app.api.routes.chat.run_chat_agent",
                return_value=self.agent_result(),
            ) as run_agent,
            patch("app.api.routes.chat.touch_session"),
        ):
            response = self.client.post(
                "/chat",
                json={
                    "thread_id": str(thread_id),
                    "message": "你好",
                    "message_request_id": str(request_id),
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["message_request_id"], str(request_id))
        self.assertFalse(response.json()["replayed"])
        self.assertEqual(
            run_agent.call_args.kwargs["message_request_id"], str(request_id)
        )

    def test_confirm_and_cancel_are_forwarded_to_safe_state_machine(self) -> None:
        for action in ("confirm", "cancel"):
            with self.subTest(action=action):
                with (
                    patch("app.api.routes.chat.validate_session_owner"),
                    patch(
                        "app.api.routes.chat.run_chat_agent",
                        return_value={
                            **self.agent_result(),
                            "success": False,
                            "code": "CHAT_OPERATION_NOT_YET_EXECUTABLE",
                        },
                    ) as run_agent,
                    patch("app.api.routes.chat.touch_session") as touch_session,
                ):
                    response = self.client.post(
                        "/chat",
                        json={
                            "thread_id": str(uuid4()),
                            "message": action,
                            "message_request_id": str(uuid4()),
                            "operation_id": str(uuid4()),
                            "task_action": action,
                            "task_revision": 0,
                        },
                    )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                run_agent.call_args.kwargs["task_action"], action
            )
            touch_session.assert_called_once()

    def test_operation_id_requires_message_request_id(self) -> None:
        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch("app.api.routes.chat.run_chat_agent") as run_agent,
            patch("app.api.routes.chat.touch_session") as touch_session,
        ):
            response = self.client.post(
                "/chat",
                json={
                    "thread_id": str(uuid4()),
                    "message": "我要请假",
                    "operation_id": str(uuid4()),
                    "task_revision": 0,
                },
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "CHAT_MESSAGE_REQUEST_ID_REQUIRED")
        run_agent.assert_not_called()
        touch_session.assert_not_called()

    def test_invalid_protocol_values_return_unified_422_without_graph(self) -> None:
        thread_id = str(uuid4())
        cases = (
            {"message_request_id": "not-a-uuid"},
            {"operation_id": "not-a-uuid"},
            {"task_action": "execute"},
            {"task_revision": -1},
            {"task_revision": "1"},
        )
        for invalid_fields in cases:
            with self.subTest(fields=invalid_fields):
                with patch("app.api.routes.chat.run_chat_agent") as run_agent:
                    response = self.client.post(
                        "/chat",
                        json={
                            "thread_id": thread_id,
                            "message": "协议测试",
                            **invalid_fields,
                        },
                        headers={"X-Request-ID": "chat-protocol-validation"},
                    )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
            self.assertEqual(
                response.headers["X-Request-ID"], "chat-protocol-validation"
            )
            run_agent.assert_not_called()

    def test_session_ownership_is_checked_before_protocol_capability(self) -> None:
        with (
            patch(
                "app.api.routes.chat.validate_session_owner",
                side_effect=SessionNotFoundError("会话不存在。"),
            ),
            patch("app.api.routes.chat.run_chat_agent") as run_agent,
        ):
            response = self.client.post(
                "/chat",
                json={
                    "thread_id": str(uuid4()),
                    "message": "确认",
                    "operation_id": str(uuid4()),
                    "task_action": "confirm",
                    "task_revision": 0,
                },
            )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["code"], "SESSION_NOT_FOUND")
        run_agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
