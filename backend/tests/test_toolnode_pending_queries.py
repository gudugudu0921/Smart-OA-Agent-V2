from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.chat_agent import (
    _collect_query_tool_result_node,
    run_chat_agent,
)
from app.models.user import User
from app.tools.query_tools import (
    build_query_hr_pending_tool,
    build_query_manager_pending_tool,
)


def user(user_id: int, role: str) -> User:
    now = datetime.now()
    return User(
        id=user_id,
        username=f"{role}_{user_id}",
        employee_no=f"PENDING{user_id:03d}",
        password_hash="not-used",
        name=f"Pending Query User {user_id}",
        role=role,
        department="Testing",
        is_active=True,
        must_change_password=False,
        created_at=now,
        updated_at=now,
    )


def tool_call(name: str, call_id: str = "call-pending", args=None) -> dict:
    return {
        "name": name,
        "args": {} if args is None else args,
        "id": call_id,
        "type": "tool_call",
    }


def recognizer(intent: str):
    return lambda _state: {
        "intent": intent,
        "extracted_info": {},
        "reference_index": None,
        "cancel_current_task": False,
        "error": None,
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


class PendingQueryToolWrapperTests(unittest.TestCase):
    def test_schemas_are_strictly_zero_argument_and_hide_trusted_context(self) -> None:
        tools = (
            build_query_manager_pending_tool(MagicMock(), user(10, "manager")),
            build_query_hr_pending_tool(MagicMock(), user(11, "hr")),
        )
        for query_tool in tools:
            with self.subTest(tool=query_tool.name):
                schema = query_tool.args_schema.model_json_schema()
                self.assertEqual(schema.get("properties"), {})
                serialized = repr(schema).lower()
                for forbidden in (
                    "db",
                    "session",
                    "user_id",
                    "manager_id",
                    "role",
                    "jwt",
                    "thread_id",
                    "operation_id",
                ):
                    self.assertNotIn(forbidden, serialized)

    def test_manager_query_uses_only_authenticated_manager_id(self) -> None:
        db = MagicMock(name="manager-db")
        manager = user(23, "manager")
        query_tool = build_query_manager_pending_tool(db, manager)
        with patch(
            "app.tools.query_tools.get_manager_pending_applications",
            return_value=[],
        ) as service:
            message = query_tool.invoke(tool_call("query_manager_pending"))
        self.assertIsInstance(message, ToolMessage)
        self.assertTrue(message.artifact["success"])
        service.assert_called_once_with(db=db, manager_id=23, limit=20)

    def test_tool_level_role_checks_reject_unauthorized_users(self) -> None:
        cases = (
            (
                build_query_manager_pending_tool,
                "query_manager_pending",
                user(30, "employee"),
                "query_manager_pending_tool",
            ),
            (
                build_query_hr_pending_tool,
                "query_hr_pending",
                user(31, "employee"),
                "query_hr_pending_tool",
            ),
            (
                build_query_hr_pending_tool,
                "query_hr_pending",
                user(32, "manager"),
                "query_hr_pending_tool",
            ),
        )
        for builder, name, current_user, adapter_name in cases:
            with self.subTest(name=name, role=current_user.role):
                with patch(f"app.tools.query_tools.{adapter_name}") as adapter:
                    message = builder(MagicMock(), current_user).invoke(
                        tool_call(name, f"call-{name}-{current_user.role}")
                    )
                self.assertIsInstance(message, ToolMessage)
                self.assertFalse(message.artifact["success"])
                self.assertEqual(message.artifact["error_type"], "permission_denied")
                adapter.assert_not_called()

    def test_request_scoped_manager_tools_do_not_mix_contexts(self) -> None:
        db_one = MagicMock(name="manager-db-one")
        db_two = MagicMock(name="manager-db-two")
        manager_one = user(41, "manager")
        manager_two = user(42, "manager")
        seen = []

        def adapter(db, current_user):
            seen.append((db, current_user.id))
            return {"success": True, "data": []}

        with patch(
            "app.tools.query_tools.query_manager_pending_tool",
            side_effect=adapter,
        ):
            tool_one = build_query_manager_pending_tool(db_one, manager_one)
            tool_two = build_query_manager_pending_tool(db_two, manager_two)
            with ThreadPoolExecutor(max_workers=2) as executor:
                messages = list(
                    executor.map(
                        lambda item: item[0].invoke(
                            tool_call("query_manager_pending", item[1])
                        ),
                        ((tool_one, "call-one"), (tool_two, "call-two")),
                    )
                )
        self.assertTrue(all(message.artifact["success"] for message in messages))
        self.assertCountEqual(seen, [(db_one, 41), (db_two, 42)])


class PendingQueryToolGraphTests(unittest.TestCase):
    def run_query(
        self,
        *,
        intent: str,
        role: str,
        model_response: object,
        adapter_result: dict | None = None,
    ):
        model = RecordingToolCallingModel(model_response)
        adapter_name = f"{intent}_tool"
        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=InMemorySaver(),
            ),
            patch("app.agents.chat_agent.get_llm", return_value=model),
            patch(
                f"app.tools.query_tools.{adapter_name}",
                return_value=adapter_result or {"success": True, "data": []},
            ) as adapter,
        ):
            result = run_chat_agent(
                MagicMock(name=f"{intent}-db"),
                user(50, role),
                str(uuid4()),
                "查询待审批请假",
                recognizer=recognizer(intent),
            )
        return result, model, adapter

    def test_manager_and_hr_queries_flow_through_toolnode_once(self) -> None:
        cases = (
            (
                "query_manager_pending",
                "manager",
                "当前没有待审批申请。",
            ),
            (
                "query_hr_pending",
                "hr",
                "当前没有待 HR 审批的申请。",
            ),
        )
        for intent, role, expected_response in cases:
            with self.subTest(intent=intent):
                call_id = f"call-{intent}"
                result, model, adapter = self.run_query(
                    intent=intent,
                    role=role,
                    model_response=AIMessage(
                        content="",
                        tool_calls=[tool_call(intent, call_id)],
                    ),
                )
                call_messages = [
                    message
                    for message in result["messages"]
                    if isinstance(message, AIMessage) and message.tool_calls
                ]
                tool_messages = [
                    message
                    for message in result["messages"]
                    if isinstance(message, ToolMessage)
                ]
                self.assertEqual(len(call_messages), 1)
                self.assertEqual(len(tool_messages), 1)
                self.assertEqual(call_messages[0].tool_calls[0]["id"], call_id)
                self.assertEqual(tool_messages[0].tool_call_id, call_id)
                self.assertEqual(tool_messages[0].name, intent)
                self.assertEqual(model.bound_tools[0].name, intent)
                self.assertEqual(len(model.bound_tools), 1)
                self.assertEqual(len(model.invocations), 1)
                self.assertEqual(result["response"], expected_response)
                self.assertTrue(result["success"])
                adapter.assert_called_once()

    def test_graph_rbac_rejects_unauthorized_pending_queries_before_llm(self) -> None:
        cases = (
            ("query_manager_pending", "employee"),
            ("query_hr_pending", "employee"),
            ("query_hr_pending", "manager"),
        )
        for intent, role in cases:
            with self.subTest(intent=intent, role=role):
                result, model, adapter = self.run_query(
                    intent=intent,
                    role=role,
                    model_response=AIMessage(
                        content="",
                        tool_calls=[tool_call(intent)],
                    ),
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "permission_denied")
                self.assertIsNone(model.bound_tools)
                adapter.assert_not_called()

    def test_invalid_tool_calls_never_execute_pending_query_adapter(self) -> None:
        cases = {
            "no_call": AIMessage(content="直接回答"),
            "wrong_name": AIMessage(
                content="",
                tool_calls=[tool_call("query_hr_pending", "call-wrong")],
            ),
            "multiple": AIMessage(
                content="",
                tool_calls=[
                    tool_call("query_manager_pending", "call-one"),
                    tool_call("query_manager_pending", "call-two"),
                ],
            ),
            "missing_id": AIMessage(
                content="",
                tool_calls=[tool_call("query_manager_pending", "")],
            ),
            "user_id_injection": AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "query_manager_pending",
                        "call-user",
                        {"user_id": 999},
                    )
                ],
            ),
            "role_injection": AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "query_manager_pending",
                        "call-role",
                        {"role": "admin"},
                    )
                ],
            ),
            "manager_id_injection": AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "query_manager_pending",
                        "call-manager",
                        {"manager_id": 999},
                    )
                ],
            ),
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                result, _model, adapter = self.run_query(
                    intent="query_manager_pending",
                    role="manager",
                    model_response=response,
                )
                self.assertFalse(result["success"])
                self.assertIn("安全执行", result["response"])
                adapter.assert_not_called()

    def test_malformed_argument_type_is_rejected_before_execution(self) -> None:
        response = AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "name": "query_manager_pending",
                    "args": "not-an-object",
                    "id": "call-malformed",
                    "error": "invalid arguments",
                    "type": "invalid_tool_call",
                }
            ],
        )
        result, _model, adapter = self.run_query(
            intent="query_manager_pending",
            role="manager",
            model_response=response,
        )
        self.assertEqual(result["error"], "invalid_tool_call_schema")
        adapter.assert_not_called()

    def test_tool_message_call_id_mismatch_is_rejected(self) -> None:
        query_tool = build_query_manager_pending_tool(
            MagicMock(),
            user(60, "manager"),
        )
        with patch("app.tools.query_tools.query_manager_pending_tool") as adapter:
            result = _collect_query_tool_result_node(
                {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                tool_call("query_manager_pending", "call-original")
                            ],
                        ),
                        ToolMessage(
                            content='{"success": true}',
                            artifact={"success": True, "data": []},
                            name="query_manager_pending",
                            tool_call_id="call-other",
                        ),
                    ]
                },
                query_tool,
            )
        self.assertEqual(result["error"], "tool_call_id_mismatch")
        adapter.assert_not_called()

    def test_toolnode_exception_returns_only_safe_error(self) -> None:
        sensitive = "mysql://private-user:secret@internal-host/private-db"

        @tool("query_manager_pending", response_format="content_and_artifact")
        def broken_query() -> tuple[str, dict]:
            """Raise an internal error for ToolNode error handling verification."""
            raise RuntimeError(sensitive)

        model = RecordingToolCallingModel(
            AIMessage(
                content="",
                tool_calls=[tool_call("query_manager_pending", "call-broken")],
            )
        )
        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=InMemorySaver(),
            ),
            patch("app.agents.chat_agent.get_llm", return_value=model),
            patch(
                "app.agents.chat_agent.build_query_manager_pending_tool",
                return_value=broken_query,
            ),
        ):
            result = run_chat_agent(
                MagicMock(),
                user(61, "manager"),
                str(uuid4()),
                "查询待审批请假",
                recognizer=recognizer("query_manager_pending"),
            )
        rendered = repr(result)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "query_failed")
        self.assertIn("查询服务暂时不可用", result["response"])
        for secret in ("private-user", "secret", "internal-host", "private-db"):
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
