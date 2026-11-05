"""Persistent state carried by the Redis-checkpointed chat graph."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


TaskPhase = Literal[
    "IDLE",
    "COLLECTING",
    "AWAITING_CONFIRMATION",
    "EXECUTING",
    "SUCCEEDED",
    "FAILED",
    "UNKNOWN",
]


class ChatState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    user_message: str
    thread_id: str

    user_id: int
    user_role: str
    user_department: str
    manager_id: int | None

    intent: str
    extracted_info: dict[str, Any]
    application_id: str | None

    tool_name: str | None
    tool_result: dict[str, Any] | None

    error: str | None
    success: bool
    response: str

    current_task: str | None
    conversation_context: str
    pending_intent: str | None
    pending_fields: list[str]
    last_query_results: list[dict[str, Any]]
    turn_count: int
    cancel_current_task: bool
    reference_index: int | None

    # Request protocol inputs. These are primitive values only and are safe to
    # serialize in a checkpoint; authenticated ORM/user objects never belong here.
    message_request_id: str | None
    request_operation_id: str | None
    request_task_action: Literal["message", "confirm", "cancel"]
    request_task_revision: int | None

    # Trusted, checkpointed task state for the submit-leave confirmation flow.
    active_operation_id: str | None
    active_operation_type: str | None
    active_task_user_id: int | None
    active_task_thread_id: str | None
    task_phase: TaskPhase
    task_revision: int
    task_payload_snapshot: dict[str, Any]
    task_fingerprint: str | None
    confirmation_fingerprint: str | None
    closed_operation_ids: list[str]

    # Per-turn public protocol projection. This is separate from the active task
    # so a cancelled task can be cleared while its cancellation response remains
    # explicit to the caller.
    response_operation_id: str | None
    response_task_status: str | None
    response_task_revision: int | None
    requires_confirmation: bool | None
    response_operation_status: Literal[
        "PROCESSING", "SUCCEEDED", "FAILED", "UNKNOWN"
    ] | None
    response_replayed: bool | None
    response_confirmation: dict[str, Any] | None
    code: str | None
