"""Safe, request-correlated lifecycle logging for LangGraph nodes and tools."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import math
import re
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.prebuilt.tool_node import ToolCallRequest

from app.core.request_context import get_request_id


logger = logging.getLogger("app.agent.tool")
node_logger = logging.getLogger("app.agent.node")

_REGISTERED_TOOL_NAMES = frozenset(
    {
        "query_my_leave",
        "query_manager_pending",
        "query_hr_pending",
        "submit_leave",
        "cancel_leave",
        "manager_review",
        "hr_review",
    }
)
_TERMINAL_STATUSES = frozenset({"success", "rejected", "error"})
_REJECTED_ERROR_TYPES = frozenset(
    {
        "permission_denied",
        "validation",
        "conflict",
        "not_found",
        "confirmation_conflict",
        "operation_in_progress",
        "operation_failed",
        "idempotency_conflict",
    }
)
_TECHNICAL_ERROR_TYPES = frozenset({"query_failed", "operation_unknown"})
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_SAFE_NODE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_GRAPH_SCOPES = frozenset({"main_chat", "confirmed_write", "task_update"})
_NODE_STATUSES = frozenset({"started", "success", "error"})


@dataclass(slots=True)
class _GraphObservationScope:
    graph_scope: str | None
    graph_run_id: str
    request_id: str | None
    thread_id: str | None
    message_request_id: str | None
    operation_id: str | None
    _event_seq: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def next_event_seq(self) -> int:
        with self._lock:
            self._event_seq += 1
            return self._event_seq


_graph_observation: ContextVar[_GraphObservationScope | None] = ContextVar(
    "graph_observation",
    default=None,
)


def _safe_request_id(value: object) -> str | None:
    text = value if isinstance(value, str) else None
    return text if text and _SAFE_REQUEST_ID.fullmatch(text) else None


def _safe_uuid(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _safe_tool_name(value: object) -> str | None:
    return value if isinstance(value, str) and value in _REGISTERED_TOOL_NAMES else None


def _safe_tool_call_id(value: object) -> str | None:
    text = value if isinstance(value, str) else None
    return text if text and _SAFE_TOOL_CALL_ID.fullmatch(text) else None


def _safe_error_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if _SAFE_ERROR_CODE.fullmatch(normalized) else None


def _safe_exception_type(value: object) -> str | None:
    text = value if isinstance(value, str) else None
    return text if text and _SAFE_EXCEPTION_TYPE.fullmatch(text) else None


def _safe_duration(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        return None
    return round(duration, 2)


def _safe_graph_scope(value: object) -> str | None:
    return value if isinstance(value, str) and value in _GRAPH_SCOPES else None


def _safe_node_name(value: object) -> str | None:
    text = value if isinstance(value, str) else None
    return text if text and _SAFE_NODE_NAME.fullmatch(text) else None


def _safe_event_seq(value: object) -> int | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > 2**63 - 1
    ):
        return None
    return value


@contextmanager
def observe_graph(
    graph_scope: str,
    *,
    thread_id: object = None,
    message_request_id: object = None,
    operation_id: object = None,
) -> Iterator[str]:
    """Create one non-persistent observation scope for a synchronous graph run."""
    parent = _graph_observation.get()
    scope = _GraphObservationScope(
        graph_scope=_safe_graph_scope(graph_scope),
        graph_run_id=str(uuid4()),
        request_id=_safe_request_id(get_request_id()),
        thread_id=_safe_uuid(thread_id) or (parent.thread_id if parent else None),
        message_request_id=_safe_uuid(message_request_id)
        or (parent.message_request_id if parent else None),
        operation_id=_safe_uuid(operation_id)
        or (parent.operation_id if parent else None),
    )
    token = _graph_observation.set(scope)
    try:
        yield scope.graph_run_id
    finally:
        _graph_observation.reset(token)


def _node_correlations(
    scope: _GraphObservationScope,
    state: object,
) -> tuple[str | None, str | None, str | None]:
    state_thread_id, state_message_request_id, state_operation_id = (
        _trusted_correlations(state)
    )
    return (
        scope.thread_id or _safe_uuid(state_thread_id),
        scope.message_request_id or _safe_uuid(state_message_request_id),
        scope.operation_id or _safe_uuid(state_operation_id),
    )


def safe_emit_node_event(
    *,
    scope: _GraphObservationScope,
    node_run_id: object,
    node_name: object,
    status: str,
    thread_id: object = None,
    message_request_id: object = None,
    operation_id: object = None,
    duration_ms: object = None,
    exception_type: object = None,
) -> None:
    """Emit one fixed-message node event without affecting graph execution."""
    try:
        if status not in _NODE_STATUSES:
            return
        extra = {
            "event_type": "langgraph_node",
            "graph_scope": _safe_graph_scope(scope.graph_scope),
            "graph_run_id": _safe_uuid(scope.graph_run_id),
            "node_run_id": _safe_uuid(node_run_id),
            "event_seq": _safe_event_seq(scope.next_event_seq()),
            "node_name": _safe_node_name(node_name),
            "status": status,
            "request_id": scope.request_id,
            "thread_id": _safe_uuid(thread_id),
            "message_request_id": _safe_uuid(message_request_id),
            "operation_id": _safe_uuid(operation_id),
            "duration_ms": _safe_duration(duration_ms),
            "exception_type": _safe_exception_type(exception_type),
        }
        node_logger.info("LangGraph node lifecycle", extra=extra)
    except Exception:
        # Observability is best-effort and must never affect or repeat a node.
        return


def observe_node(
    node_name: str,
    node: Callable[[Any], Any] | Runnable,
) -> Callable[[Any, RunnableConfig | None], Any]:
    """Wrap one synchronous node while preserving its input and return value."""

    def observed_node(
        state: Any,
        config: RunnableConfig | None = None,
    ) -> Any:
        scope = _graph_observation.get()
        if scope is None:
            if isinstance(node, Runnable):
                return node.invoke(state, config)
            return node(state)

        node_run_id = str(uuid4())
        thread_id, message_request_id, operation_id = _node_correlations(
            scope,
            state,
        )
        started_at = perf_counter()
        safe_emit_node_event(
            scope=scope,
            node_run_id=node_run_id,
            node_name=node_name,
            status="started",
            thread_id=thread_id,
            message_request_id=message_request_id,
            operation_id=operation_id,
        )
        try:
            if isinstance(node, Runnable):
                result = node.invoke(state, config)
            else:
                result = node(state)
        except Exception as exc:
            safe_emit_node_event(
                scope=scope,
                node_run_id=node_run_id,
                node_name=node_name,
                status="error",
                thread_id=thread_id,
                message_request_id=message_request_id,
                operation_id=operation_id,
                duration_ms=(perf_counter() - started_at) * 1000,
                exception_type=type(exc).__name__,
            )
            raise

        result_thread_id, result_message_request_id, result_operation_id = (
            _node_correlations(scope, result)
        )
        safe_emit_node_event(
            scope=scope,
            node_run_id=node_run_id,
            node_name=node_name,
            status="success",
            thread_id=result_thread_id or thread_id,
            message_request_id=result_message_request_id or message_request_id,
            operation_id=result_operation_id or operation_id,
            duration_ms=(perf_counter() - started_at) * 1000,
        )
        return result

    # LangGraph 1.0 inspects the concrete annotation object rather than resolving
    # postponed annotations when deciding whether to inject RunnableConfig.
    observed_node.__annotations__["config"] = RunnableConfig | None
    return observed_node


def safe_emit_agent_event(
    *,
    status: str,
    tool_name: object,
    tool_call_id: object,
    thread_id: object = None,
    message_request_id: object = None,
    operation_id: object = None,
    duration_ms: object = None,
    error_code: object = None,
    exception_type: object = None,
) -> None:
    """Emit one fixed-message event; observability must never affect the tool."""
    try:
        if status not in _TERMINAL_STATUSES and status != "started":
            return
        extra = {
            "event_type": "agent_tool",
            "status": status,
            "request_id": _safe_request_id(get_request_id()),
            "tool_name": _safe_tool_name(tool_name),
            "tool_call_id": _safe_tool_call_id(tool_call_id),
            "thread_id": _safe_uuid(thread_id),
            "message_request_id": _safe_uuid(message_request_id),
            "operation_id": _safe_uuid(operation_id),
            "duration_ms": _safe_duration(duration_ms),
            "error_code": _safe_error_code(error_code),
            "exception_type": _safe_exception_type(exception_type),
        }
        logger.info("Agent tool call lifecycle", extra=extra)
    except Exception:
        # Logging must remain strictly best-effort and must never trigger a retry.
        return


def _tool_message(result: object) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result
    if isinstance(result, list):
        messages = [item for item in result if isinstance(item, ToolMessage)]
        if len(messages) == 1:
            return messages[0]
    return None


def _classify_tool_result(result: object) -> tuple[str, str | None]:
    message = _tool_message(result)
    if message is None:
        return "error", "UNRECOGNIZED_TOOL_RESULT"
    if message.status == "error":
        return "error", "TOOL_EXECUTION_ERROR"

    artifact = message.artifact
    if not isinstance(artifact, dict):
        return "error", "UNRECOGNIZED_TOOL_RESULT"
    error_type = artifact.get("error_type")
    safe_code = _safe_error_code(error_type)
    if artifact.get("operation_status") == "UNKNOWN":
        return "error", safe_code or "OPERATION_RESULT_UNKNOWN"
    if artifact.get("success") is True:
        return "success", None
    if artifact.get("success") is not False:
        return "error", "UNRECOGNIZED_TOOL_RESULT"
    if error_type in _REJECTED_ERROR_TYPES:
        return "rejected", safe_code
    if error_type in _TECHNICAL_ERROR_TYPES:
        return "error", safe_code
    return "error", safe_code or "UNRECOGNIZED_TOOL_RESULT"


def _trusted_correlations(state: object) -> tuple[object, object, object]:
    if not isinstance(state, dict):
        return None, None, None
    # active_operation_id is already bound by the task protocol. Deliberately do
    # not log the unbound request_operation_id candidate used by read-only turns.
    return (
        state.get("thread_id"),
        state.get("message_request_id"),
        state.get("active_operation_id"),
    )


def build_tool_call_observer() -> Callable[[ToolCallRequest, Callable], Any]:
    """Return a ToolNode wrapper that executes each call exactly once."""

    def observe_tool_call(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Any],
    ) -> Any:
        call = request.tool_call
        tool_name = call.get("name")
        tool_call_id = call.get("id")
        thread_id, message_request_id, operation_id = _trusted_correlations(
            request.state
        )
        started_at = perf_counter()
        safe_emit_agent_event(
            status="started",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
            message_request_id=message_request_id,
            operation_id=operation_id,
        )
        try:
            result = execute(request)
        except Exception as exc:
            safe_emit_agent_event(
                status="error",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                thread_id=thread_id,
                message_request_id=message_request_id,
                operation_id=operation_id,
                duration_ms=(perf_counter() - started_at) * 1000,
                error_code="TOOL_EXECUTION_ERROR",
                exception_type=type(exc).__name__,
            )
            raise

        try:
            status, error_code = _classify_tool_result(result)
        except Exception:
            status, error_code = "error", "UNRECOGNIZED_TOOL_RESULT"
        safe_emit_agent_event(
            status=status,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
            message_request_id=message_request_id,
            operation_id=operation_id,
            duration_ms=(perf_counter() - started_at) * 1000,
            error_code=error_code,
        )
        return result

    return observe_tool_call
