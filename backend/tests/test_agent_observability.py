from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agents.chat_state import ChatState
from app.agents.observability import build_tool_call_observer
from app.core.logging import JsonLogFormatter
from app.core.request_context import reset_request_id, set_request_id
from app.tools.approval_tools import build_hr_review_tool, build_manager_review_tool
from app.tools.leave_tools import build_cancel_leave_tool, build_submit_leave_tool
from app.tools.query_tools import (
    build_query_hr_pending_tool,
    build_query_manager_pending_tool,
    build_query_my_leave_tool,
)


def _call(name: str, call_id: str, args: dict | None = None) -> dict:
    return {
        "name": name,
        "args": args or {},
        "id": call_id,
        "type": "tool_call",
    }


def _state(call: dict) -> dict:
    return {
        "messages": [AIMessage(content="", tool_calls=[call])],
        "thread_id": str(uuid4()),
        "message_request_id": str(uuid4()),
        "active_operation_id": str(uuid4()),
    }


def _invoke(node: ToolNode, state: dict) -> dict:
    graph = StateGraph(ChatState)
    graph.add_node("observed_tool", node)
    graph.add_edge(START, "observed_tool")
    graph.add_edge("observed_tool", END)
    return graph.compile().invoke(state)


def _result_tool_message(result: dict) -> ToolMessage:
    messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    if len(messages) != 1:
        raise AssertionError(f"expected one ToolMessage, got {len(messages)}")
    return messages[0]


class _LogCapture:
    def __init__(self) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(JsonLogFormatter())
        self.logger = logging.getLogger("app.agent.tool")
        self.old_level = self.logger.level
        self.old_propagate = self.logger.propagate

    def __enter__(self) -> "_LogCapture":
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)
        self.logger.propagate = self.old_propagate

    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


class _FailingHandler(logging.Handler):
    def __init__(self, fail_on: int) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.calls = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("logging sink secret must not escape")


class ToolCallObserverTests(unittest.TestCase):
    def test_success_emits_one_started_and_one_success_without_mutating_message(self) -> None:
        calls = 0

        @tool("query_my_leave", response_format="content_and_artifact")
        def query_my_leave() -> tuple[str, dict]:
            """Return one isolated successful query result."""
            nonlocal calls
            calls += 1
            artifact = {"success": True, "data": []}
            return json.dumps(artifact), artifact

        call_id = "call-success-1"
        node = ToolNode(
            [query_my_leave],
            handle_tool_errors="safe tool error",
            wrap_tool_call=build_tool_call_observer(),
        )
        token = set_request_id("request-tool-success")
        try:
            with _LogCapture() as capture:
                result = _invoke(node, _state(_call(query_my_leave.name, call_id)))
        finally:
            reset_request_id(token)

        message = _result_tool_message(result)
        self.assertIsInstance(message, ToolMessage)
        self.assertEqual(message.tool_call_id, call_id)
        self.assertEqual(message.name, query_my_leave.name)
        self.assertEqual(message.status, "success")
        self.assertEqual(message.artifact, {"success": True, "data": []})
        self.assertEqual(calls, 1)

        entries = capture.entries()
        self.assertEqual([entry["status"] for entry in entries], ["started", "success"])
        self.assertTrue(all(entry["event_type"] == "agent_tool" for entry in entries))
        self.assertTrue(all(entry["tool_call_id"] == call_id for entry in entries))
        self.assertTrue(all(entry["request_id"] == "request-tool-success" for entry in entries))
        self.assertGreaterEqual(entries[-1]["duration_ms"], 0)

    def test_business_rejection_is_rejected_and_artifact_is_unchanged(self) -> None:
        artifact = {
            "success": False,
            "error_type": "permission_denied",
            "message": "safe rejection",
        }

        @tool("query_my_leave", response_format="content_and_artifact")
        def query_my_leave() -> tuple[str, dict]:
            """Return one isolated business rejection."""
            return json.dumps(artifact), artifact

        node = ToolNode(
            [query_my_leave],
            handle_tool_errors="safe tool error",
            wrap_tool_call=build_tool_call_observer(),
        )
        with _LogCapture() as capture:
            result = _invoke(node, _state(_call(query_my_leave.name, "call-rejected")))

        message = _result_tool_message(result)
        self.assertEqual(message.status, "success")
        self.assertEqual(message.artifact, artifact)
        terminal = capture.entries()[-1]
        self.assertEqual(terminal["status"], "rejected")
        self.assertEqual(terminal["error_code"], "PERMISSION_DENIED")

    def test_tool_exception_uses_original_toolnode_error_message_without_leaking(self) -> None:
        sensitive = "jwt=secret-token mysql://private api-key=NeverEcho"

        @tool("query_my_leave", response_format="content_and_artifact")
        def query_my_leave() -> tuple[str, dict]:
            """Raise one isolated infrastructure failure."""
            raise RuntimeError(sensitive)

        node = ToolNode(
            [query_my_leave],
            handle_tool_errors="查询服务暂时不可用",
            wrap_tool_call=build_tool_call_observer(),
        )
        with _LogCapture() as capture:
            result = _invoke(node, _state(_call(query_my_leave.name, "call-error")))

        message = _result_tool_message(result)
        self.assertEqual(message.status, "error")
        self.assertEqual(message.content, "查询服务暂时不可用")
        terminal = capture.entries()[-1]
        self.assertEqual(terminal["status"], "error")
        self.assertEqual(terminal["error_code"], "TOOL_EXECUTION_ERROR")
        rendered = capture.stream.getvalue()
        for forbidden in ("secret-token", "mysql://", "NeverEcho"):
            self.assertNotIn(forbidden, rendered)

    def test_unhandled_tool_exception_still_propagates(self) -> None:
        error = RuntimeError("sensitive business failure")

        @tool("query_my_leave")
        def query_my_leave() -> str:
            """Raise one exception with ToolNode handling disabled."""
            raise error

        node = ToolNode(
            [query_my_leave],
            handle_tool_errors=False,
            wrap_tool_call=build_tool_call_observer(),
        )
        with _LogCapture() as capture:
            with self.assertRaises(RuntimeError) as raised:
                _invoke(node, _state(_call(query_my_leave.name, "call-propagated")))
        self.assertIs(raised.exception, error)
        terminal = capture.entries()[-1]
        self.assertEqual(terminal["status"], "error")
        self.assertEqual(terminal["exception_type"], "RuntimeError")
        self.assertNotIn("sensitive business failure", capture.stream.getvalue())

    def test_unknown_result_and_unknown_operation_are_never_success(self) -> None:
        cases = (
            ({"success": False, "error_type": "operation_unknown", "operation_status": "UNKNOWN"}, "OPERATION_UNKNOWN"),
            ({"unexpected": True}, "UNRECOGNIZED_TOOL_RESULT"),
        )
        for index, (artifact, expected_code) in enumerate(cases):
            with self.subTest(artifact=artifact):

                @tool("query_my_leave", response_format="content_and_artifact")
                def query_my_leave() -> tuple[str, dict]:
                    """Return one non-success result shape."""
                    return json.dumps(artifact), artifact

                node = ToolNode(
                    [query_my_leave],
                    handle_tool_errors="safe tool error",
                    wrap_tool_call=build_tool_call_observer(),
                )
                with _LogCapture() as capture:
                    _invoke(node, _state(_call(query_my_leave.name, f"call-unknown-{index}")))
                terminal = capture.entries()[-1]
                self.assertEqual(terminal["status"], "error")
                self.assertEqual(terminal["error_code"], expected_code)

    def test_logging_failures_never_retry_or_block_the_tool(self) -> None:
        for fail_on in (1, 2):
            with self.subTest(fail_on=fail_on):
                calls = 0

                @tool("query_my_leave", response_format="content_and_artifact")
                def query_my_leave() -> tuple[str, dict]:
                    """Count execution while the log sink fails."""
                    nonlocal calls
                    calls += 1
                    artifact = {"success": True, "data": []}
                    return "ok", artifact

                node = ToolNode(
                    [query_my_leave],
                    handle_tool_errors="safe tool error",
                    wrap_tool_call=build_tool_call_observer(),
                )
                target = logging.getLogger("app.agent.tool")
                handler = _FailingHandler(fail_on)
                old_level, old_propagate = target.level, target.propagate
                target.setLevel(logging.INFO)
                target.propagate = False
                target.addHandler(handler)
                try:
                    result = _invoke(
                        node,
                        _state(_call(query_my_leave.name, f"call-log-failure-{fail_on}"))
                    )
                finally:
                    target.removeHandler(handler)
                    target.setLevel(old_level)
                    target.propagate = old_propagate
                self.assertEqual(calls, 1)
                self.assertTrue(_result_tool_message(result).artifact["success"])

    def test_result_classification_failure_does_not_change_or_retry_tool(self) -> None:
        calls = 0

        @tool("query_my_leave", response_format="content_and_artifact")
        def query_my_leave() -> tuple[str, dict]:
            """Return success while the observer classifier fails."""
            nonlocal calls
            calls += 1
            return "ok", {"success": True, "data": []}

        node = ToolNode(
            [query_my_leave],
            handle_tool_errors="safe tool error",
            wrap_tool_call=build_tool_call_observer(),
        )
        with (
            patch(
                "app.agents.observability._classify_tool_result",
                side_effect=RuntimeError("observer-only failure"),
            ),
            _LogCapture() as capture,
        ):
            result = _invoke(node, _state(_call(query_my_leave.name, "call-classify")))
        self.assertEqual(calls, 1)
        self.assertTrue(_result_tool_message(result).artifact["success"])
        terminal = capture.entries()[-1]
        self.assertEqual(terminal["status"], "error")
        self.assertEqual(terminal["error_code"], "UNRECOGNIZED_TOOL_RESULT")
        self.assertNotIn("observer-only failure", capture.stream.getvalue())

    def test_sensitive_arguments_and_unsafe_call_id_never_enter_logs(self) -> None:
        unsafe_call_id = "jwt=secret token/" + "x" * 200
        sensitive_argument = "password=NeverEcho api_key=PrivateKey"

        @tool("query_my_leave", response_format="content_and_artifact")
        def query_my_leave(value: str) -> tuple[str, dict]:
            """Accept a sensitive test value without logging it."""
            artifact = {"success": True, "data": [value]}
            return value, artifact

        node = ToolNode(
            [query_my_leave],
            handle_tool_errors="safe tool error",
            wrap_tool_call=build_tool_call_observer(),
        )
        with _LogCapture() as capture:
            result = _invoke(
                node,
                _state(
                    _call(
                        query_my_leave.name,
                        unsafe_call_id,
                        {"value": sensitive_argument},
                    )
                )
            )
        self.assertEqual(_result_tool_message(result).tool_call_id, unsafe_call_id)
        rendered = capture.stream.getvalue()
        self.assertNotIn("NeverEcho", rendered)
        self.assertNotIn("PrivateKey", rendered)
        self.assertNotIn("jwt=secret", rendered)
        self.assertTrue(all("tool_call_id" not in item for item in capture.entries()))

    def test_request_id_is_present_in_context_and_null_without_context(self) -> None:
        @tool("query_my_leave", response_format="content_and_artifact")
        def query_my_leave() -> tuple[str, dict]:
            """Return a safe result for request context checks."""
            return "ok", {"success": True, "data": []}

        node = ToolNode(
            [query_my_leave],
            handle_tool_errors="safe tool error",
            wrap_tool_call=build_tool_call_observer(),
        )
        token = set_request_id("request-context-tool")
        try:
            with _LogCapture() as with_context:
                _invoke(node, _state(_call(query_my_leave.name, "call-context")))
        finally:
            reset_request_id(token)
        with _LogCapture() as without_context:
            _invoke(node, _state(_call(query_my_leave.name, "call-no-context")))

        self.assertTrue(
            all(item["request_id"] == "request-context-tool" for item in with_context.entries())
        )
        self.assertTrue(
            all(item["request_id"] is None for item in without_context.entries())
        )


class SevenToolCompatibilityTests(unittest.TestCase):
    def test_all_seven_request_scoped_tools_use_the_same_observer_shape(self) -> None:
        application_id = str(uuid4())
        operation_id = str(uuid4())
        unprivileged = SimpleNamespace(id=99, role="admin")
        submit_payload = {
            "leave_type": "annual",
            "start_date": "2035-06-01",
            "end_date": "2035-06-02",
            "reason": "compatibility test",
        }
        review_payload = {
            "application_id": application_id,
            "decision": "approved",
            "comment": None,
        }
        tools_and_args = (
            (build_query_my_leave_tool(MagicMock(), unprivileged), {}),
            (build_query_manager_pending_tool(MagicMock(), unprivileged), {}),
            (build_query_hr_pending_tool(MagicMock(), unprivileged), {}),
            (
                build_submit_leave_tool(
                    MagicMock(),
                    unprivileged,
                    operation_id=operation_id,
                    confirmed_payload=submit_payload,
                ),
                submit_payload,
            ),
            (
                build_cancel_leave_tool(
                    MagicMock(),
                    unprivileged,
                    operation_id=operation_id,
                    confirmed_payload={"application_id": application_id},
                ),
                {"application_id": application_id},
            ),
            (
                build_manager_review_tool(
                    MagicMock(),
                    unprivileged,
                    operation_id=operation_id,
                    confirmed_payload=review_payload,
                ),
                review_payload,
            ),
            (
                build_hr_review_tool(
                    MagicMock(),
                    unprivileged,
                    operation_id=operation_id,
                    confirmed_payload=review_payload,
                ),
                review_payload,
            ),
        )

        terminal_events = []
        with _LogCapture() as capture:
            for current_tool, args in tools_and_args:
                call_id = f"call-{current_tool.name}"
                node = ToolNode(
                    [current_tool],
                    handle_tool_errors="safe tool error",
                    wrap_tool_call=build_tool_call_observer(),
                )
                result = _invoke(node, _state(_call(current_tool.name, call_id, args)))
                message = _result_tool_message(result)
                self.assertEqual(message.name, current_tool.name)
                self.assertEqual(message.tool_call_id, call_id)
                self.assertFalse(message.artifact["success"])
            terminal_events = [
                item for item in capture.entries() if item["status"] != "started"
            ]

        self.assertEqual(len(terminal_events), 7)
        self.assertEqual(
            {item["tool_name"] for item in terminal_events},
            {
                "query_my_leave",
                "query_manager_pending",
                "query_hr_pending",
                "submit_leave",
                "cancel_leave",
                "manager_review",
                "hr_review",
            },
        )
        self.assertTrue(all(item["status"] == "rejected" for item in terminal_events))


if __name__ == "__main__":
    unittest.main()
