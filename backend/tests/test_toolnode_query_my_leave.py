from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.chat_agent import (
    QUERY_MY_LEAVE_TOOL_NAME,
    _validate_query_tool_call_node,
    run_chat_agent,
)
from app.api.routes.chat import chat
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services.session_service import get_session_messages
from app.tools.query_tools import build_query_my_leave_tool


def user(user_id: int, role: str = "employee") -> User:
    now = datetime.now()
    return User(
        id=user_id,
        username=f"{role}_{user_id}",
        employee_no=f"PHASE1{user_id:03d}",
        password_hash="not-used",
        name=f"Phase 1 User {user_id}",
        role=role,
        department="Testing",
        is_active=True,
        must_change_password=False,
        created_at=now,
        updated_at=now,
    )


def query_call(call_id: str = "call-query-1", **changes: object) -> dict:
    value = {
        "name": QUERY_MY_LEAVE_TOOL_NAME,
        "args": {},
        "id": call_id,
        "type": "tool_call",
    }
    value.update(changes)
    return value


def query_recognizer(_state) -> dict:
    return {
        "intent": "query_my_leave",
        "extracted_info": {},
        "reference_index": None,
        "cancel_current_task": False,
        "error": None,
    }


def leave_item(application_id: str | None = None) -> dict:
    return {
        "application_id": application_id or str(uuid4()),
        "leave_type": "annual",
        "start_date": "2027-01-10",
        "end_date": "2027-01-11",
        "leave_days": 2.0,
        "status": "pending",
        "manager_status": "pending",
        "hr_status": "pending",
    }


class RecordingToolCallingModel:
    def __init__(self, response: object) -> None:
        self.response = response
        self.bound_tools = None
        self.invocations = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return self.response


class QueryToolWrapperTests(unittest.TestCase):
    def test_schema_does_not_expose_trusted_context(self) -> None:
        query_tool = build_query_my_leave_tool(MagicMock(), user(10))
        schema = query_tool.args_schema.model_json_schema()
        self.assertEqual(schema.get("properties"), {})
        serialized = repr(schema).lower()
        for forbidden in ("db", "current_user", "user_id", "role", "session"):
            self.assertNotIn(forbidden, serialized)

    def test_authenticated_employee_id_is_the_only_query_scope(self) -> None:
        db = MagicMock(name="employee-db")
        employee = user(23)
        query_tool = build_query_my_leave_tool(db, employee)
        with patch(
            "app.tools.query_tools.get_my_leave_applications",
            return_value=[],
        ) as service:
            message = query_tool.invoke(query_call())
        service.assert_called_once_with(db=db, employee_id=23, limit=20)
        self.assertIsInstance(message, ToolMessage)
        self.assertTrue(message.artifact["success"])

    def test_non_employee_roles_are_rejected_inside_tool(self) -> None:
        with patch("app.tools.query_tools.query_my_leave_tool") as adapter:
            for index, role in enumerate(("manager", "hr", "admin"), start=1):
                with self.subTest(role=role):
                    query_tool = build_query_my_leave_tool(MagicMock(), user(index, role))
                    message = query_tool.invoke(query_call(f"call-{role}"))
                    self.assertIsInstance(message, ToolMessage)
                    self.assertFalse(message.artifact["success"])
                    self.assertEqual(
                        message.artifact["error_type"], "permission_denied"
                    )
        adapter.assert_not_called()

    def test_request_scoped_tools_do_not_mix_concurrent_contexts(self) -> None:
        db_one = MagicMock(name="db-one")
        db_two = MagicMock(name="db-two")
        user_one = user(31)
        user_two = user(32)
        seen = []

        def adapter(db, current_user):
            seen.append((db, current_user.id))
            return {"success": True, "data": []}

        with patch("app.tools.query_tools.query_my_leave_tool", side_effect=adapter):
            tool_one = build_query_my_leave_tool(db_one, user_one)
            tool_two = build_query_my_leave_tool(db_two, user_two)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(tool_one.invoke, query_call("call-one")),
                    executor.submit(tool_two.invoke, query_call("call-two")),
                ]
                for future in futures:
                    self.assertTrue(future.result().artifact["success"])

        self.assertCountEqual(seen, [(db_one, 31), (db_two, 32)])


class QueryToolGraphTests(unittest.TestCase):
    def run_query(
        self,
        model_response: object,
        *,
        query_result: dict | None = None,
        query_error: Exception | None = None,
        saver: InMemorySaver | None = None,
        thread_id: str | None = None,
        current_user: User | None = None,
        operation_id: str | None = None,
        task_revision: int | None = None,
    ):
        model = RecordingToolCallingModel(model_response)
        checkpointer = saver or InMemorySaver()
        adapter_kwargs = (
            {"side_effect": query_error}
            if query_error is not None
            else {"return_value": query_result or {"success": True, "data": []}}
        )
        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=checkpointer,
            ),
            patch("app.agents.chat_agent.get_llm", return_value=model),
            patch(
                "app.tools.query_tools.query_my_leave_tool",
                **adapter_kwargs,
            ) as adapter,
        ):
            result = run_chat_agent(
                MagicMock(name="request-db"),
                current_user or user(40),
                thread_id or str(uuid4()),
                "查询我的请假记录",
                recognizer=query_recognizer,
                operation_id=operation_id,
                task_revision=task_revision,
            )
        return result, model, adapter, checkpointer

    def test_real_ai_tool_call_flows_through_toolnode_to_toolmessage(self) -> None:
        call_id = "call-real-structure"
        result, model, adapter, _ = self.run_query(
            AIMessage(content="", tool_calls=[query_call(call_id)]),
            query_result={"success": True, "data": [leave_item()]},
        )

        call_messages = [
            message
            for message in result["messages"]
            if isinstance(message, AIMessage) and message.tool_calls
        ]
        tool_messages = [
            message for message in result["messages"] if isinstance(message, ToolMessage)
        ]
        self.assertEqual(len(call_messages), 1)
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(call_messages[0].tool_calls[0]["id"], call_id)
        self.assertEqual(tool_messages[0].tool_call_id, call_id)
        self.assertEqual(tool_messages[0].name, QUERY_MY_LEAVE_TOOL_NAME)
        self.assertEqual(tool_messages[0].artifact, result["tool_result"])
        self.assertTrue(result["success"])
        self.assertEqual(len(result["tool_result"]["data"]), 1)
        self.assertEqual(model.bound_tools[0].name, QUERY_MY_LEAVE_TOOL_NAME)
        self.assertEqual(len(model.invocations), 1)
        adapter.assert_called_once()

    def test_unbound_candidate_is_ignored_by_read_only_toolnode_query(self) -> None:
        operation_id = str(uuid4())
        call_id = "call-candidate-query"
        result, model, adapter, _ = self.run_query(
            AIMessage(content="", tool_calls=[query_call(call_id)]),
            query_result={"success": True, "data": [leave_item()]},
            operation_id=operation_id,
            task_revision=0,
        )

        call_message = next(
            message
            for message in result["messages"]
            if isinstance(message, AIMessage) and message.tool_calls
        )
        tool_message = next(
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
        )
        self.assertEqual(call_message.tool_calls[0]["id"], call_id)
        self.assertEqual(tool_message.tool_call_id, call_id)
        self.assertTrue(result["success"])
        self.assertEqual(result["task_phase"], "IDLE")
        self.assertIsNone(result["active_operation_id"])
        self.assertIsNone(result.get("response_operation_id"))
        self.assertEqual(model.bound_tools[0].name, QUERY_MY_LEAVE_TOOL_NAME)
        adapter.assert_called_once()

    def test_invalid_model_tool_calls_are_rejected_before_execution(self) -> None:
        cases = {
            "no_call": AIMessage(content="直接回答，不调用工具"),
            "multiple": AIMessage(
                content="",
                tool_calls=[query_call("call-1"), query_call("call-2")],
            ),
            "unknown": AIMessage(
                content="",
                tool_calls=[query_call("call-unknown", name="cancel_leave")],
            ),
            "extra_argument": AIMessage(
                content="",
                tool_calls=[query_call("call-args", args={"user_id": 999})],
            ),
            "missing_id": AIMessage(content="", tool_calls=[query_call("")]),
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                result, _, adapter, _ = self.run_query(response)
                self.assertFalse(result["success"])
                self.assertIn("安全执行", result["response"])
                self.assertIsNone(result.get("tool_result"))
                adapter.assert_not_called()

    def test_malformed_tool_call_is_rejected(self) -> None:
        response = AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "name": QUERY_MY_LEAVE_TOOL_NAME,
                    "args": "not-json",
                    "id": "call-invalid",
                    "error": "invalid arguments",
                    "type": "invalid_tool_call",
                }
            ],
        )
        result, _, adapter, _ = self.run_query(response)
        self.assertEqual(result["error"], "invalid_tool_call_schema")
        adapter.assert_not_called()

    def test_validation_rejects_missing_call_id_without_toolnode(self) -> None:
        state = {
            "messages": [AIMessage(content="", tool_calls=[query_call("")])]
        }
        result = _validate_query_tool_call_node(state)
        self.assertEqual(result["error"], "missing_tool_call_id")

    def test_query_exception_returns_only_safe_error(self) -> None:
        sensitive = "mysql://private-user:secret@internal-host/private-path"
        result, _, _, _ = self.run_query(
            AIMessage(content="", tool_calls=[query_call("call-failure")]),
            query_error=RuntimeError(sensitive),
        )
        rendered = repr(result)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "query_failed")
        self.assertIn("查询服务暂时不可用", result["response"])
        self.assertNotIn("private-user", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("internal-host", rendered)
        self.assertNotIn("private-path", rendered)

    def test_checkpoint_restores_tool_call_and_tool_message_association(self) -> None:
        saver = InMemorySaver()
        thread_id = str(uuid4())
        first_id = "call-checkpoint"
        first, _, _, _ = self.run_query(
            AIMessage(content="", tool_calls=[query_call(first_id)]),
            saver=saver,
            thread_id=thread_id,
        )
        self.assertTrue(first["success"])

        with patch(
            "app.agents.chat_agent.get_redis_checkpointer", return_value=saver
        ):
            restored = run_chat_agent(
                MagicMock(),
                user(40),
                thread_id,
                "你好",
                recognizer=lambda _state: {
                    "intent": "general_chat",
                    "extracted_info": {},
                    "error": None,
                },
            )

        call_message = next(
            message
            for message in restored["messages"]
            if isinstance(message, AIMessage) and message.tool_calls
        )
        tool_message = next(
            message
            for message in restored["messages"]
            if isinstance(message, ToolMessage)
        )
        self.assertEqual(call_message.tool_calls[0]["id"], first_id)
        self.assertEqual(tool_message.tool_call_id, first_id)

    def test_pending_submit_task_can_switch_to_query_without_breaking_toolnode(self) -> None:
        saver = InMemorySaver()
        thread_id = str(uuid4())
        first_recognizer = lambda _state: {
            "intent": "submit_leave",
            "extracted_info": {"leave_type": "annual"},
            "error": None,
        }
        with patch(
            "app.agents.chat_agent.get_redis_checkpointer", return_value=saver
        ):
            operation_id = str(uuid4())
            pending = run_chat_agent(
                MagicMock(),
                user(41),
                thread_id,
                "我要请年假",
                recognizer=first_recognizer,
                message_request_id=str(uuid4()),
                operation_id=operation_id,
                task_revision=0,
            )
        self.assertEqual(pending["pending_intent"], "submit_leave")

        switched, _, _, _ = self.run_query(
            AIMessage(content="", tool_calls=[query_call("call-after-old-state")]),
            saver=saver,
            thread_id=thread_id,
            current_user=user(41),
            operation_id=operation_id,
            task_revision=pending["task_revision"],
        )
        self.assertTrue(switched["success"])
        self.assertIsNone(switched["pending_intent"])
        self.assertEqual(switched["pending_fields"], [])
        self.assertIsNone(switched["current_task"])
        self.assertGreaterEqual(len(switched["messages"]), 6)

class QueryToolApiAndHistoryTests(unittest.TestCase):
    def test_session_history_hides_tool_messages_and_tool_call_ai(self) -> None:
        call_id = "call-hidden"
        checkpoint = SimpleNamespace(
            checkpoint={
                "channel_values": {
                    "messages": [
                        HumanMessage(content="查询我的请假"),
                        AIMessage(content="", tool_calls=[query_call(call_id)]),
                        AIMessage(
                            content="内部模型文本不得展示",
                            additional_kwargs={"smart_oa_internal_tool_call": True},
                        ),
                        ToolMessage(
                            content='{"success": true}',
                            artifact={"success": True, "data": []},
                            name=QUERY_MY_LEAVE_TOOL_NAME,
                            tool_call_id=call_id,
                        ),
                        AIMessage(content="当前没有请假记录。"),
                        AIMessage(content=""),
                    ]
                }
            }
        )
        checkpointer = MagicMock()
        checkpointer.get_tuple.return_value = checkpoint
        with (
            patch("app.services.session_service.validate_session_owner"),
            patch(
                "app.services.session_service.get_redis_checkpointer",
                return_value=checkpointer,
            ),
        ):
            messages = get_session_messages(70, str(uuid4()))
        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "查询我的请假"},
                {"role": "assistant", "content": "当前没有请假记录。"},
            ],
        )

    def test_chat_api_response_contract_is_unchanged(self) -> None:
        current_user = user(71)
        thread_id = uuid4()
        result = {
            "success": True,
            "intent": "query_my_leave",
            "response": "当前没有请假记录。",
            "tool_result": {"success": True, "data": []},
        }
        turn_lock = MagicMock()
        turn_lock.__enter__.return_value = turn_lock
        turn_lock.__exit__.return_value = None
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/chat",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
            }
        )
        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch(
                "app.api.routes.chat.acquire_chat_turn_lock",
                return_value=turn_lock,
            ),
            patch("app.api.routes.chat.touch_session"),
            patch("app.api.routes.chat.run_chat_agent", return_value=result),
        ):
            response = chat(
                ChatRequest(thread_id=thread_id, message="查询我的请假"),
                request,
                db=object(),
                current_user=current_user,
                idempotency_session_factory=MagicMock(),
            )
        self.assertEqual(
            response.model_dump(mode="json"),
            {
                "success": True,
                "thread_id": str(thread_id),
                "intent": "query_my_leave",
                "response": "当前没有请假记录。",
                "data": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
