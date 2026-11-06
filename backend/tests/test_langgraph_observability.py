from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import io
import json
import logging
from threading import Barrier
import unittest
from typing import TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agents.chat_state import ChatState
from app.agents.observability import (
    build_tool_call_observer,
    observe_graph,
    observe_node,
)
from app.core.logging import JsonLogFormatter
from app.core.request_context import reset_request_id, set_request_id


class _MiniState(TypedDict):
    value: int


class _ParallelState(TypedDict, total=False):
    left: int
    right: int


class _LogCapture:
    def __init__(self, *logger_names: str) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(JsonLogFormatter())
        self.loggers = [logging.getLogger(name) for name in logger_names]
        self.old_states = [
            (logger.level, logger.propagate) for logger in self.loggers
        ]

    def __enter__(self) -> "_LogCapture":
        for logger in self.loggers:
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        for logger, (old_level, old_propagate) in zip(
            self.loggers,
            self.old_states,
            strict=True,
        ):
            logger.removeHandler(self.handler)
            logger.setLevel(old_level)
            logger.propagate = old_propagate

    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


class _FailingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.calls += 1
        raise RuntimeError("logging sink secret")


def _uuid() -> str:
    return str(uuid4())


class LangGraphNodeObserverTests(unittest.TestCase):
    def test_success_preserves_return_and_emits_started_then_success(self) -> None:
        returned = object()
        calls = 0

        def node(_state: dict) -> object:
            nonlocal calls
            calls += 1
            return returned

        wrapped = observe_node("plain_node", node)
        thread_id = _uuid()
        message_request_id = _uuid()
        operation_id = _uuid()
        token = set_request_id("request-node-success")
        try:
            with _LogCapture("app.agent.node") as capture:
                with observe_graph(
                    "main_chat",
                    thread_id=thread_id,
                    message_request_id=message_request_id,
                    operation_id=operation_id,
                ) as graph_run_id:
                    result = wrapped({"sensitive": "prompt-secret"})
        finally:
            reset_request_id(token)

        self.assertIs(result, returned)
        self.assertEqual(calls, 1)
        entries = capture.entries()
        self.assertEqual([entry["status"] for entry in entries], ["started", "success"])
        self.assertEqual([entry["event_seq"] for entry in entries], [1, 2])
        self.assertTrue(all(entry["event_type"] == "langgraph_node" for entry in entries))
        self.assertTrue(all(entry["graph_scope"] == "main_chat" for entry in entries))
        self.assertTrue(all(entry["graph_run_id"] == graph_run_id for entry in entries))
        self.assertEqual(len({entry["node_run_id"] for entry in entries}), 1)
        self.assertTrue(all(entry["node_name"] == "plain_node" for entry in entries))
        self.assertTrue(all(entry["request_id"] == "request-node-success" for entry in entries))
        self.assertTrue(all(entry["thread_id"] == thread_id for entry in entries))
        self.assertTrue(
            all(entry["message_request_id"] == message_request_id for entry in entries)
        )
        self.assertTrue(all(entry["operation_id"] == operation_id for entry in entries))
        self.assertNotIn("prompt-secret", capture.stream.getvalue())

    def test_exception_is_logged_safely_and_re_raised_unchanged(self) -> None:
        expected = ValueError("exception-message-secret")

        def node(_state: dict) -> None:
            raise expected

        wrapped = observe_node("failing_node", node)
        with _LogCapture("app.agent.node") as capture:
            with observe_graph("main_chat"):
                with self.assertRaises(ValueError) as raised:
                    wrapped({"password": "input-secret"})

        self.assertIs(raised.exception, expected)
        entries = capture.entries()
        self.assertEqual([entry["status"] for entry in entries], ["started", "error"])
        self.assertEqual(entries[-1]["exception_type"], "ValueError")
        output = capture.stream.getvalue()
        self.assertNotIn("exception-message-secret", output)
        self.assertNotIn("input-secret", output)
        self.assertNotIn("password", output)

    def test_logging_failure_does_not_repeat_or_change_node(self) -> None:
        calls = 0

        def node(_state: dict) -> str:
            nonlocal calls
            calls += 1
            return "unchanged"

        wrapped = observe_node("logging_failure_node", node)
        target = logging.getLogger("app.agent.node")
        handler = _FailingHandler()
        old_level, old_propagate = target.level, target.propagate
        target.setLevel(logging.INFO)
        target.propagate = False
        target.addHandler(handler)
        try:
            with observe_graph("main_chat"):
                result = wrapped({})
        finally:
            target.removeHandler(handler)
            target.setLevel(old_level)
            target.propagate = old_propagate

        self.assertEqual(result, "unchanged")
        self.assertEqual(calls, 1)
        self.assertEqual(handler.calls, 2)

    def test_repeated_node_calls_have_distinct_node_run_ids(self) -> None:
        wrapped = observe_node("repeat_node", lambda state: state)
        with _LogCapture("app.agent.node") as capture:
            with observe_graph("main_chat") as graph_run_id:
                first = wrapped({"value": 1})
                second = wrapped({"value": 2})

        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 2})
        entries = capture.entries()
        self.assertEqual([entry["event_seq"] for entry in entries], [1, 2, 3, 4])
        self.assertTrue(all(entry["graph_run_id"] == graph_run_id for entry in entries))
        node_run_ids = [entry["node_run_id"] for entry in entries]
        self.assertEqual(node_run_ids[0], node_run_ids[1])
        self.assertEqual(node_run_ids[2], node_run_ids[3])
        self.assertNotEqual(node_run_ids[0], node_run_ids[2])

    def test_event_sequence_is_unique_under_concurrency(self) -> None:
        workers = 8
        barrier = Barrier(workers)

        def node(value: int) -> int:
            barrier.wait(timeout=5)
            return value

        wrapped = observe_node("parallel_node", node)
        token = set_request_id("request-parallel")
        try:
            with _LogCapture("app.agent.node") as capture:
                with observe_graph("main_chat") as graph_run_id:
                    contexts = [copy_context() for _ in range(workers)]
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = [
                            executor.submit(context.run, wrapped, index)
                            for index, context in enumerate(contexts)
                        ]
                        results = [future.result(timeout=5) for future in futures]
        finally:
            reset_request_id(token)

        self.assertEqual(results, list(range(workers)))
        entries = capture.entries()
        sequences = [entry["event_seq"] for entry in entries]
        self.assertEqual(sorted(sequences), list(range(1, workers * 2 + 1)))
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertTrue(all(entry["graph_run_id"] == graph_run_id for entry in entries))
        self.assertTrue(all(entry["request_id"] == "request-parallel" for entry in entries))
        self.assertEqual(len({entry["node_run_id"] for entry in entries}), workers)

    def test_langgraph_parallel_nodes_keep_scope_and_request_context(self) -> None:
        barrier = Barrier(2)

        def left_node(_state: _ParallelState) -> _ParallelState:
            barrier.wait(timeout=5)
            return {"left": 1}

        def right_node(_state: _ParallelState) -> _ParallelState:
            barrier.wait(timeout=5)
            return {"right": 2}

        graph = StateGraph(_ParallelState)
        graph.add_node("left_node", observe_node("left_node", left_node))
        graph.add_node("right_node", observe_node("right_node", right_node))
        graph.add_edge(START, "left_node")
        graph.add_edge(START, "right_node")
        graph.add_edge("left_node", END)
        graph.add_edge("right_node", END)
        compiled = graph.compile()
        token = set_request_id("request-langgraph-parallel")
        try:
            with _LogCapture("app.agent.node") as capture:
                with observe_graph("main_chat") as graph_run_id:
                    result = compiled.invoke({})
        finally:
            reset_request_id(token)

        self.assertEqual(result, {"left": 1, "right": 2})
        entries = capture.entries()
        self.assertEqual(len(entries), 4)
        self.assertEqual(
            sorted(entry["event_seq"] for entry in entries),
            [1, 2, 3, 4],
        )
        self.assertTrue(all(entry["graph_run_id"] == graph_run_id for entry in entries))
        self.assertTrue(
            all(
                entry["request_id"] == "request-langgraph-parallel"
                for entry in entries
            )
        )
        self.assertEqual({entry["node_name"] for entry in entries}, {"left_node", "right_node"})

    def test_concurrent_request_ids_and_graph_runs_do_not_mix(self) -> None:
        barrier = Barrier(2)
        wrapped = observe_node("request_isolation_node", lambda value: barrier.wait() or value)

        def worker(request_id: str) -> str:
            token = set_request_id(request_id)
            try:
                with observe_graph("main_chat"):
                    wrapped(request_id)
            finally:
                reset_request_id(token)
            return request_id

        with _LogCapture("app.agent.node") as capture:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(worker, "request-one"),
                    executor.submit(worker, "request-two"),
                ]
                self.assertEqual(
                    {future.result(timeout=5) for future in futures},
                    {"request-one", "request-two"},
                )

        entries = capture.entries()
        self.assertEqual(
            {entry["request_id"] for entry in entries},
            {"request-one", "request-two"},
        )
        graph_ids_by_request = {
            request_id: {
                entry["graph_run_id"]
                for entry in entries
                if entry["request_id"] == request_id
            }
            for request_id in ("request-one", "request-two")
        }
        self.assertTrue(
            all(len(ids) == 1 for ids in graph_ids_by_request.values())
        )
        self.assertTrue(
            graph_ids_by_request["request-one"].isdisjoint(
                graph_ids_by_request["request-two"]
            )
        )

    def test_nested_scopes_restore_parent_and_do_not_leak(self) -> None:
        wrapped = observe_node("nested_node", lambda state: state)
        with _LogCapture("app.agent.node") as capture:
            with observe_graph("main_chat") as main_graph_run_id:
                wrapped({"value": 1})
                with observe_graph("confirmed_write") as write_graph_run_id:
                    wrapped({"value": 2})
                wrapped({"value": 3})
            wrapped({"value": 4})

        entries = capture.entries()
        self.assertEqual(len(entries), 6)
        main_entries = [
            entry for entry in entries if entry["graph_run_id"] == main_graph_run_id
        ]
        write_entries = [
            entry for entry in entries if entry["graph_run_id"] == write_graph_run_id
        ]
        self.assertEqual([entry["event_seq"] for entry in main_entries], [1, 2, 3, 4])
        self.assertEqual([entry["event_seq"] for entry in write_entries], [1, 2])
        self.assertTrue(all(entry["graph_scope"] == "main_chat" for entry in main_entries))
        self.assertTrue(
            all(entry["graph_scope"] == "confirmed_write" for entry in write_entries)
        )

    def test_no_request_context_logs_null_request_id(self) -> None:
        wrapped = observe_node("no_request_node", lambda state: state)
        with _LogCapture("app.agent.node") as capture:
            with observe_graph("task_update"):
                wrapped({})

        self.assertEqual(
            [entry["request_id"] for entry in capture.entries()],
            [None, None],
        )

    def test_bound_state_operation_id_is_used_but_candidate_is_not(self) -> None:
        active_operation_id = _uuid()
        wrapped = observe_node("correlation_node", lambda state: state)
        with _LogCapture("app.agent.node") as capture:
            with observe_graph("main_chat"):
                wrapped(
                    {
                        "active_operation_id": active_operation_id,
                        "request_operation_id": _uuid(),
                    }
                )

        self.assertTrue(
            all(
                entry["operation_id"] == active_operation_id
                for entry in capture.entries()
            )
        )

    def test_tool_node_and_tool_call_events_are_distinct_and_not_duplicated(self) -> None:
        calls = 0

        @tool("query_my_leave", response_format="content_and_artifact")
        def query_my_leave() -> tuple[str, dict]:
            """Return one safe isolated result."""
            nonlocal calls
            calls += 1
            artifact = {"success": True, "data": []}
            return json.dumps(artifact), artifact

        tool_node = ToolNode(
            [query_my_leave],
            handle_tool_errors="safe tool error",
            wrap_tool_call=build_tool_call_observer(),
        )
        wrapped = observe_node("query_my_leave_tool", tool_node)
        call_id = "call-node-tool-1"
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": query_my_leave.name,
                            "args": {},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "thread_id": _uuid(),
            "message_request_id": _uuid(),
            "active_operation_id": _uuid(),
        }
        with _LogCapture("app.agent.node", "app.agent.tool") as capture:
            with observe_graph("main_chat"):
                graph = StateGraph(ChatState)
                graph.add_node("query_my_leave_tool", wrapped)
                graph.add_edge(START, "query_my_leave_tool")
                graph.add_edge("query_my_leave_tool", END)
                result = graph.compile().invoke(state)

        self.assertEqual(calls, 1)
        messages = [
            message for message in result["messages"] if isinstance(message, ToolMessage)
        ]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].tool_call_id, call_id)
        entries = capture.entries()
        node_entries = [
            entry for entry in entries if entry["event_type"] == "langgraph_node"
        ]
        tool_entries = [
            entry for entry in entries if entry["event_type"] == "agent_tool"
        ]
        self.assertEqual([entry["status"] for entry in node_entries], ["started", "success"])
        self.assertEqual([entry["status"] for entry in tool_entries], ["started", "success"])
        self.assertEqual(len(tool_entries), 2)

    def test_tool_business_rejection_is_not_a_node_exception(self) -> None:
        @tool("query_my_leave", response_format="content_and_artifact")
        def query_my_leave() -> tuple[str, dict]:
            """Return one safe business rejection."""
            artifact = {
                "success": False,
                "error_type": "validation",
                "message": "safe rejection",
            }
            return json.dumps(artifact), artifact

        tool_node = ToolNode(
            [query_my_leave],
            handle_tool_errors="safe tool error",
            wrap_tool_call=build_tool_call_observer(),
        )
        wrapped = observe_node("query_my_leave_tool", tool_node)
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": query_my_leave.name,
                            "args": {},
                            "id": "call-node-rejected-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "thread_id": _uuid(),
            "message_request_id": _uuid(),
        }
        graph = StateGraph(ChatState)
        graph.add_node("query_my_leave_tool", wrapped)
        graph.add_edge(START, "query_my_leave_tool")
        graph.add_edge("query_my_leave_tool", END)

        with _LogCapture("app.agent.node", "app.agent.tool") as capture:
            with observe_graph("main_chat"):
                graph.compile().invoke(state)

        entries = capture.entries()
        node_statuses = [
            entry["status"]
            for entry in entries
            if entry["event_type"] == "langgraph_node"
        ]
        tool_statuses = [
            entry["status"]
            for entry in entries
            if entry["event_type"] == "agent_tool"
        ]
        self.assertEqual(node_statuses, ["started", "success"])
        self.assertEqual(tool_statuses, ["started", "rejected"])

    def test_in_memory_checkpoint_contains_no_observation_fields(self) -> None:
        checkpointer = InMemorySaver()
        graph = StateGraph(_MiniState)
        graph.add_node(
            "increment",
            observe_node("increment", lambda state: {"value": state["value"] + 1}),
        )
        graph.add_edge(START, "increment")
        graph.add_edge("increment", END)
        compiled = graph.compile(checkpointer=checkpointer)
        thread_id = _uuid()
        config = {"configurable": {"thread_id": thread_id}}

        with _LogCapture("app.agent.node"):
            with observe_graph("main_chat", thread_id=thread_id):
                result = compiled.invoke({"value": 1}, config=config)

        self.assertEqual(result, {"value": 2})
        checkpoint = checkpointer.get_tuple(config)
        self.assertIsNotNone(checkpoint)
        channel_values = checkpoint.checkpoint["channel_values"]
        forbidden = {
            "graph_scope",
            "graph_run_id",
            "node_run_id",
            "event_seq",
            "node_name",
            "request_id",
        }
        self.assertTrue(forbidden.isdisjoint(result))
        self.assertTrue(forbidden.isdisjoint(channel_values))


if __name__ == "__main__":
    unittest.main()
