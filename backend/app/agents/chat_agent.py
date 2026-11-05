"""Redis-checkpointed single-agent LangGraph workflow."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
import time
from typing import Any
from uuid import UUID

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError
from redis.exceptions import RedisError, WatchError
from sqlalchemy.orm import Session

from app.agents.chat_state import ChatState
from app.agents.recognition import recognize_intent
from app.core.llm import LLMConfigurationError, LLMUnavailableError, get_llm
from app.db.redis import (
    Redis7CheckpointSaver,
    RedisUnavailableError,
    get_redis_checkpointer,
)
from app.models.leave_application import LeaveApplication
from app.models.user import User
from app.schemas.leave import LeaveCreate, LeaveReviewRequest
from app.services.idempotency_service import (
    FINGERPRINT_VERSION,
    IdempotencyConflictError,
    OperationNotFoundError,
    OperationResultUnknownError,
    calculate_request_fingerprint,
    get_operation_for_actor,
    get_safe_operation_status,
)
from app.services.leave_service import (
    LeaveConflictError,
    LeaveNotFoundError,
    LeavePermissionError,
    SessionFactory,
    get_cancellable_leave_application,
    get_hr_reviewable_leave_application,
    get_manager_reviewable_leave_application,
)
from app.services.session_service import (
    CHAT_TURN_LOCK_TTL_SECONDS,
    ChatTurnLock,
    ChatTurnLockLostError,
)
from app.tools.approval_tools import build_hr_review_tool, build_manager_review_tool
from app.tools.leave_tools import build_cancel_leave_tool, build_submit_leave_tool
from app.tools.query_tools import (
    build_query_hr_pending_tool,
    build_query_manager_pending_tool,
    build_query_my_leave_tool,
)


SUPPORTED_INTENTS = {
    "submit_leave",
    "query_my_leave",
    "cancel_leave",
    "query_manager_pending",
    "manager_review",
    "query_hr_pending",
    "hr_review",
    "general_chat",
}
ALLOWED_INTENTS_BY_ROLE = {
    "employee": {"submit_leave", "query_my_leave", "cancel_leave", "general_chat"},
    "manager": {"query_manager_pending", "manager_review", "general_chat"},
    "hr": {"query_hr_pending", "hr_review", "general_chat"},
    "admin": {"general_chat"},
}
REQUIRED_FIELDS = {
    "submit_leave": ("leave_type", "start_date", "end_date"),
    "cancel_leave": ("application_id",),
    "manager_review": ("application_id", "decision"),
    "hr_review": ("application_id", "decision"),
}
MISSING_FIELD_LABELS = {
    "leave_type": "请假类型",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "application_id": "申请编号",
    "decision": "审批决定",
}
LEAVE_TYPE_LABELS = {
    "annual": "年假",
    "sick": "病假",
    "personal": "事假",
    "marriage": "婚假",
    "maternity": "产假",
    "bereavement": "丧假",
    "other": "其他",
}
QUERY_MY_LEAVE_TOOL_NAME = "query_my_leave"
READ_ONLY_QUERY_INTENTS = {
    "query_my_leave",
    "query_manager_pending",
    "query_hr_pending",
}
SUBMIT_LEAVE_OPERATION = "SUBMIT_LEAVE"
CANCEL_LEAVE_OPERATION = "CANCEL_LEAVE"
MANAGER_REVIEW_OPERATION = "MANAGER_REVIEW"
HR_REVIEW_OPERATION = "HR_REVIEW"
CONFIRMABLE_OPERATION_TYPES = {
    SUBMIT_LEAVE_OPERATION,
    CANCEL_LEAVE_OPERATION,
    MANAGER_REVIEW_OPERATION,
    HR_REVIEW_OPERATION,
}
TASK_PHASES = {
    "IDLE",
    "COLLECTING",
    "AWAITING_CONFIRMATION",
    "EXECUTING",
    "SUCCEEDED",
    "FAILED",
    "UNKNOWN",
}
MUTABLE_TASK_PHASES = {"COLLECTING", "AWAITING_CONFIRMATION"}
QUERY_MY_LEAVE_TOOL_PROMPT = """你正在执行已经由后端识别并授权的“查询我的请假记录”操作。
必须且只能调用一次 query_my_leave 工具。该工具没有任何参数。
不要添加 user_id、role、数据库信息或其他参数，不要改为调用其他工具，也不要直接回答查询结果。"""
QUERY_MANAGER_PENDING_TOOL_PROMPT = """你正在执行已经由后端识别并授权的“查询主管待审批请假”操作。
必须且只能调用一次 query_manager_pending 工具。该工具没有任何参数。
不要添加 user_id、manager_id、role、数据库信息或其他参数，不要改为调用其他工具，也不要直接回答查询结果。"""
QUERY_HR_PENDING_TOOL_PROMPT = """你正在执行已经由后端识别并授权的“查询 HR 待审批请假”操作。
必须且只能调用一次 query_hr_pending 工具。该工具没有任何参数。
不要添加 user_id、role、数据库信息或其他参数，不要改为调用其他工具，也不要直接回答查询结果。"""


@dataclass(frozen=True)
class AgentExecutionContext:
    db: Session
    current_user: User
    ip_address: str | None


class _TurnBufferedCheckpointSaver(BaseCheckpointSaver[int]):
    """Buffer one turn and atomically publish its final checkpoint under the lock.

    LangGraph normally persists after every node. A renewable lease alone cannot
    stop a process that was paused longer than the TTL from resuming and writing
    stale checkpoints. This saver reads the previous Redis checkpoint normally,
    buffers all writes for the current turn, and publishes only the final state in
    a Redis WATCH transaction that verifies the exact lock owner.
    """

    def __init__(
        self,
        delegate: Redis7CheckpointSaver,
        turn_lock: ChatTurnLock,
    ) -> None:
        super().__init__(serde=delegate.serde)
        self.delegate = delegate
        self.turn_lock = turn_lock
        self._base_checkpoint_id = ""
        self._pending: tuple[
            RunnableConfig,
            Checkpoint,
            CheckpointMetadata,
            ChannelVersions,
        ] | None = None

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        item = self.delegate.get_tuple(config)
        if item is not None:
            self._base_checkpoint_id = item.checkpoint["id"]
        return item

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        yield from self.delegate.list(
            config,
            filter=filter,
            before=before,
            limit=limit,
        )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._pending = (config, checkpoint, metadata, new_versions)
        configurable = config["configurable"]
        return {
            "configurable": {
                "thread_id": str(configurable["thread_id"]),
                "checkpoint_ns": str(configurable.get("checkpoint_ns", "")),
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        # A completed turn's final checkpoint contains the applied channel state.
        # If the turn fails, nothing is published and its message receipt remains
        # conservatively PROCESSING/unknown rather than resuming partial work.
        del config, writes, task_id, task_path

    @staticmethod
    def _owner_text(raw_owner: bytes | str | None) -> str | None:
        if isinstance(raw_owner, bytes):
            return raw_owner.decode()
        return raw_owner

    def flush(self) -> None:
        if self._pending is None:
            return
        config, checkpoint, metadata, _new_versions = self._pending
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = checkpoint["id"]
        checkpoint_type, checkpoint_payload = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_payload = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        checkpoint_key = self.delegate._checkpoint_key(
            thread_id, checkpoint_ns, checkpoint_id
        )
        index_key = self.delegate._index_key(thread_id, checkpoint_ns)
        latest_key = self.delegate._latest_key(thread_id, checkpoint_ns)
        thread_keys_key = self.delegate._thread_keys_key(thread_id)
        writes_key = self.delegate._writes_key(
            thread_id, checkpoint_ns, checkpoint_id
        )
        mapping = {
            "checkpoint_type": checkpoint_type,
            "checkpoint": checkpoint_payload,
            "metadata_type": metadata_type,
            "metadata": metadata_payload,
            "parent_id": self._base_checkpoint_id,
        }

        client = self.delegate.client
        try:
            with client.pipeline(transaction=True) as pipe:
                pipe.watch(self.turn_lock.key)
                owner = self._owner_text(pipe.get(self.turn_lock.key))
                if owner != self.turn_lock.owner:
                    pipe.unwatch()
                    raise ChatTurnLockLostError(
                        "Chat turn lock was lost before checkpoint publication"
                    )
                pipe.multi()
                pipe.hset(checkpoint_key, mapping=mapping)
                pipe.zadd(index_key, {checkpoint_id: time.time()})
                pipe.set(latest_key, checkpoint_id)
                pipe.sadd(
                    thread_keys_key,
                    checkpoint_key,
                    index_key,
                    latest_key,
                    writes_key,
                )
                pipe.expire(
                    self.turn_lock.key,
                    CHAT_TURN_LOCK_TTL_SECONDS,
                )
                pipe.execute()
        except WatchError as exc:
            raise ChatTurnLockLostError(
                "Chat turn lock changed during checkpoint publication"
            ) from exc
        except RedisError as exc:
            raise RedisUnavailableError("Redis 会话服务当前不可用。") from exc
        self._pending = None


def _cleared_submit_task() -> dict[str, Any]:
    return {
        "active_operation_id": None,
        "active_operation_type": None,
        "active_task_user_id": None,
        "active_task_thread_id": None,
        "task_phase": "IDLE",
        "task_revision": 0,
        "task_payload_snapshot": {},
        "task_fingerprint": None,
        "confirmation_fingerprint": None,
        "requires_confirmation": False,
        "response_confirmation": None,
    }


def _close_submit_task(state: ChatState) -> dict[str, Any]:
    closed = list(state.get("closed_operation_ids", []))
    operation_id = state.get("active_operation_id")
    if operation_id and operation_id not in closed:
        closed.append(operation_id)
    return {**_cleared_submit_task(), "closed_operation_ids": closed}


def _switch_from_submit_task(state: ChatState) -> dict[str, Any]:
    operation_id = state.get("active_operation_id")
    return {
        **_close_submit_task(state),
        "response_operation_id": operation_id,
        "response_task_status": "cancelled",
        "response_task_revision": state.get("task_revision", 0),
        "code": "CHAT_TASK_SWITCHED",
    }


def _protocol_failure(
    state: ChatState,
    *,
    code: str,
    response: str,
    task_status: str | None = None,
) -> dict[str, Any]:
    return {
        "intent": state.get("pending_intent") or state.get("intent") or "general_chat",
        "error": code,
        "code": code,
        "success": False,
        "response": response,
        "response_operation_id": state.get("active_operation_id")
        or state.get("request_operation_id"),
        "response_task_status": task_status,
        "response_task_revision": state.get("task_revision"),
        "requires_confirmation": state.get("task_phase")
        == "AWAITING_CONFIRMATION",
    }


def _normalize_task_state_node(state: ChatState) -> dict[str, Any]:
    """Adapt old checkpoints and reject stale task protocol before recognition."""
    updates: dict[str, Any] = {}
    phase = state.get("task_phase")
    if phase not in TASK_PHASES:
        phase = (
            "COLLECTING"
            if state.get("pending_intent") in {
                "submit_leave",
                "cancel_leave",
                "manager_review",
            }
            else "IDLE"
        )
        updates["task_phase"] = phase
    if "task_revision" not in state:
        updates["task_revision"] = 0
    if "task_payload_snapshot" not in state:
        updates["task_payload_snapshot"] = {}
    if "closed_operation_ids" not in state:
        updates["closed_operation_ids"] = []
    for field in (
        "active_operation_id",
        "active_operation_type",
        "active_task_user_id",
        "active_task_thread_id",
        "task_fingerprint",
        "confirmation_fingerprint",
    ):
        if field not in state:
            updates[field] = None

    active_operation_id = state.get("active_operation_id")
    request_operation_id = state.get("request_operation_id")
    request_revision = state.get("request_task_revision")
    current_revision = state.get("task_revision", 0)

    if active_operation_id is not None:
        task_user_id = state.get("active_task_user_id")
        task_thread_id = state.get("active_task_thread_id")
        if task_user_id not in (None, state.get("user_id")) or task_thread_id not in (
            None,
            state.get("thread_id"),
        ):
            return {
                **updates,
                **_protocol_failure(
                    state,
                    code="CHAT_TASK_OWNERSHIP_CONFLICT",
                    response="当前任务与认证用户或会话不匹配。",
                    task_status=phase.lower(),
                ),
            }

    action = state.get("request_task_action", "message")
    if action != "message":
        return updates

    if active_operation_id is not None:
        if phase in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            if request_revision not in (None, 0):
                return {
                    **updates,
                    **_protocol_failure(
                        state,
                        code="CHAT_TASK_REVISION_CONFLICT",
                        response="新的聊天任务必须从 task_revision 0 开始。",
                        task_status=phase.lower(),
                    ),
                }
            # Terminal tasks are immutable. Retire their operation id before the
            # next ordinary message so a new independent task can start safely.
            return {**updates, **_close_submit_task(state)}
        if phase not in MUTABLE_TASK_PHASES:
            return {
                **updates,
                **_protocol_failure(
                    state,
                    code="CHAT_TASK_NOT_MUTABLE",
                    response="当前任务状态不允许继续修改，请先查询业务操作状态。",
                    task_status=phase.lower(),
                ),
            }
        if request_operation_id is None:
            return {
                **updates,
                **_protocol_failure(
                    state,
                    code="CHAT_OPERATION_ID_REQUIRED",
                    response="继续当前请假任务时必须提供原 operation_id。",
                    task_status=phase.lower(),
                ),
            }
        if request_operation_id != active_operation_id:
            return {
                **updates,
                **_protocol_failure(
                    state,
                    code="CHAT_OPERATION_ID_CONFLICT",
                    response="当前会话已有另一笔未完成任务，请先取消或完成该任务。",
                    task_status=phase.lower(),
                ),
            }
        if request_revision is None or request_revision != current_revision:
            return {
                **updates,
                **_protocol_failure(
                    state,
                    code="CHAT_TASK_REVISION_CONFLICT",
                    response="任务版本已变化，请刷新会话后使用最新 task_revision。",
                    task_status=phase.lower(),
                ),
            }
    elif request_revision not in (None, 0):
        return {
            **updates,
            **_protocol_failure(
                state,
                code="CHAT_TASK_REVISION_CONFLICT",
                response="新任务的 task_revision 必须从 0 开始。",
            ),
        }
    return updates


def _after_task_protocol(state: ChatState) -> str:
    if state.get("error"):
        return "generate_response"
    action = state.get("request_task_action", "message")
    if action == "confirm":
        return "confirm_submit_leave"
    if action == "cancel":
        return "cancel_submit_leave_task"
    return "recognize_intent"


def _leave_task_payload(extracted: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in ("leave_type", "start_date", "end_date", "reason"):
        value = extracted.get(field)
        payload[field] = value.isoformat() if isinstance(value, date) else value
    return payload


def _task_fingerprint(
    payload: dict[str, Any],
    operation_type: str = SUBMIT_LEAVE_OPERATION,
) -> str:
    return calculate_request_fingerprint(operation_type, payload)


def _confirmation_payload(
    payload: dict[str, Any],
    revision: int,
    operation_type: str = SUBMIT_LEAVE_OPERATION,
) -> dict[str, Any]:
    """Validate and normalize a snapshot before exposing it to a client."""
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("task revision must be a non-negative integer")
    if not isinstance(payload, dict):
        raise ValueError("confirmation snapshot must be an object")
    if operation_type == SUBMIT_LEAVE_OPERATION:
        try:
            validated = LeaveCreate.model_validate(payload)
        except ValidationError as exc:
            raise ValueError("confirmation snapshot is invalid") from exc
        normalized = validated.model_dump(mode="json")
    elif operation_type == CANCEL_LEAVE_OPERATION:
        expected_fields = {
            "application_id",
            "leave_type",
            "start_date",
            "end_date",
            "status",
        }
        if set(payload) != expected_fields or payload.get("status") != "pending":
            raise ValueError("cancellation confirmation snapshot is invalid")
        try:
            normalized = {
                "application_id": str(UUID(str(payload["application_id"]))),
                "leave_type": str(payload["leave_type"]),
                "start_date": date.fromisoformat(str(payload["start_date"])).isoformat(),
                "end_date": date.fromisoformat(str(payload["end_date"])).isoformat(),
                "status": "pending",
            }
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("cancellation confirmation snapshot is invalid") from exc
        if normalized["leave_type"] not in LEAVE_TYPE_LABELS:
            raise ValueError("cancellation confirmation snapshot is invalid")
    elif operation_type in {MANAGER_REVIEW_OPERATION, HR_REVIEW_OPERATION}:
        expected_fields = {
            "application_id",
            "employee_name",
            "employee_no",
            "leave_type",
            "start_date",
            "end_date",
            "status",
            "decision",
            "comment",
        }
        if set(payload) != expected_fields or payload.get("status") != "pending":
            raise ValueError("review confirmation snapshot is invalid")
        try:
            review = LeaveReviewRequest.model_validate(
                {
                    "decision": payload["decision"],
                    "comment": payload["comment"],
                }
            )
            normalized = {
                "application_id": str(UUID(str(payload["application_id"]))),
                "employee_name": str(payload["employee_name"]),
                "employee_no": str(payload["employee_no"]),
                "leave_type": str(payload["leave_type"]),
                "start_date": date.fromisoformat(str(payload["start_date"])).isoformat(),
                "end_date": date.fromisoformat(str(payload["end_date"])).isoformat(),
                "status": "pending",
                **review.model_dump(mode="json"),
            }
        except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as exc:
            raise ValueError("review confirmation snapshot is invalid") from exc
        if (
            not normalized["employee_name"]
            or not normalized["employee_no"]
            or normalized["leave_type"] not in LEAVE_TYPE_LABELS
        ):
            raise ValueError("review confirmation snapshot is invalid")
    else:
        raise ValueError("unsupported confirmation operation type")
    if payload != normalized:
        raise ValueError("confirmation snapshot is not canonically normalized")
    confirmation = {**normalized, "task_revision": revision}
    if operation_type in {
        CANCEL_LEAVE_OPERATION,
        MANAGER_REVIEW_OPERATION,
        HR_REVIEW_OPERATION,
    }:
        confirmation["operation_type"] = operation_type
    return confirmation


def _confirmation_summary(
    payload: dict[str, Any],
    revision: int,
    operation_type: str = SUBMIT_LEAVE_OPERATION,
) -> str:
    confirmation = _confirmation_payload(payload, revision, operation_type)
    if operation_type == CANCEL_LEAVE_OPERATION:
        leave_type_value = confirmation["leave_type"]
        leave_type = LEAVE_TYPE_LABELS.get(leave_type_value, leave_type_value)
        return (
            f"请确认撤销请假申请（版本 {revision}）：\n"
            f"申请编号：{confirmation['application_id']}\n"
            f"类型：{leave_type}\n"
            f"日期：{confirmation['start_date']} 至 {confirmation['end_date']}\n"
            "确认后将撤销该申请；如目标不正确，请取消当前任务。"
        )
    if operation_type in {MANAGER_REVIEW_OPERATION, HR_REVIEW_OPERATION}:
        leave_type_value = confirmation["leave_type"]
        leave_type = LEAVE_TYPE_LABELS.get(leave_type_value, leave_type_value)
        decision = "同意" if confirmation["decision"] == "approved" else "拒绝"
        comment = confirmation["comment"] or "未填写"
        is_hr_review = operation_type == HR_REVIEW_OPERATION
        next_state = (
            (
                "确认后该申请将进入批准终态"
                if confirmation["decision"] == "approved"
                else "确认后该申请将进入拒绝终态"
            )
            if is_hr_review
            else (
                "确认后将进入 HR 审批阶段"
                if confirmation["decision"] == "approved"
                else "确认后该申请将进入拒绝终态"
            )
        )
        return (
            f"请确认{'HR 终审' if is_hr_review else '主管审批'}（版本 {revision}）：\n"
            f"申请编号：{confirmation['application_id']}\n"
            f"员工：{confirmation['employee_name']}（{confirmation['employee_no']}）\n"
            f"类型：{leave_type}\n"
            f"日期：{confirmation['start_date']} 至 {confirmation['end_date']}\n"
            f"决定：{decision}\n"
            f"意见：{comment}\n"
            f"{next_state}；如目标或决定不正确，请取消当前任务。"
        )
    leave_type_value = confirmation["leave_type"]
    leave_type = LEAVE_TYPE_LABELS.get(leave_type_value, leave_type_value)
    reason = confirmation["reason"] or "未填写"
    return (
        f"请确认请假信息（版本 {revision}）：\n"
        f"类型：{leave_type}\n"
        f"日期：{confirmation['start_date']} 至 {confirmation['end_date']}\n"
        f"原因：{reason}\n"
        "确认无误后请使用确认操作提交；如需修改，请直接说明新参数。"
    )


def _validate_task_snapshot(
    state: ChatState,
    expected_operation_type: str | None = None,
) -> tuple[str, int, dict[str, Any]]:
    operation_id = state.get("active_operation_id")
    revision = state.get("task_revision")
    snapshot = state.get("task_payload_snapshot")
    expected = state.get("confirmation_fingerprint")
    operation_type = state.get("active_operation_type")
    if (
        not isinstance(operation_id, str)
        or not isinstance(revision, int)
        or not isinstance(snapshot, dict)
        or not expected
        or operation_type not in CONFIRMABLE_OPERATION_TYPES
        or (
            expected_operation_type is not None
            and operation_type != expected_operation_type
        )
        or state.get("task_fingerprint") != expected
        or _task_fingerprint(snapshot, operation_type) != expected
    ):
        raise IdempotencyConflictError("Chat confirmation snapshot is invalid")
    return operation_id, revision, snapshot


def project_chat_task_state(
    state: dict[str, Any], *, user_id: int, thread_id: str
) -> dict[str, Any]:
    """Build a conservative public view of an already-persisted checkpoint."""
    phase = state.get("task_phase")
    operation_id = state.get("active_operation_id")
    revision = state.get("task_revision", 0)
    valid_revision = (
        isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0
    )

    if operation_id is None:
        if phase == "COLLECTING" or state.get("pending_intent") in {
            "submit_leave",
            "cancel_leave",
            "manager_review",
        }:
            return {
                "operation_id": None,
                "task_status": "collecting",
                "task_revision": revision if valid_revision else 0,
                "requires_confirmation": False,
                "confirmation": None,
                "operation_status": None,
                "code": "CHAT_TASK_PROTOCOL_UPGRADE_REQUIRED",
            }
        return {
            "operation_id": None,
            "task_status": "idle",
            "task_revision": 0,
            "requires_confirmation": False,
            "confirmation": None,
            "operation_status": None,
            "code": "CHAT_TASK_IDLE",
        }

    try:
        UUID(str(operation_id))
    except (TypeError, ValueError, AttributeError):
        operation_id = None

    bindings_are_safe = (
        operation_id is not None
        and valid_revision
        and state.get("active_operation_type") in CONFIRMABLE_OPERATION_TYPES
        and state.get("active_task_user_id") == user_id
        and state.get("active_task_thread_id") == thread_id
        and phase in TASK_PHASES
        and phase != "IDLE"
    )
    if not bindings_are_safe:
        return {
            "operation_id": None,
            "task_status": "unknown",
            "task_revision": 0,
            "requires_confirmation": False,
            "confirmation": None,
            "operation_status": None,
            "code": "CHAT_TASK_STATE_UNSAFE",
        }

    confirmation = None
    requires_confirmation = False
    code_by_phase = {
        "COLLECTING": "CHAT_TASK_COLLECTING",
        "AWAITING_CONFIRMATION": "CHAT_CONFIRMATION_REQUIRED",
        "EXECUTING": "CHAT_OPERATION_STATUS_REQUIRES_LOOKUP",
        "SUCCEEDED": "CHAT_OPERATION_STATUS_REQUIRES_LOOKUP",
        "FAILED": "CHAT_OPERATION_STATUS_REQUIRES_LOOKUP",
        "UNKNOWN": "CHAT_OPERATION_STATUS_REQUIRES_LOOKUP",
    }
    if phase == "AWAITING_CONFIRMATION":
        try:
            validated_operation_id, validated_revision, snapshot = (
                _validate_task_snapshot(state)
            )
            if (
                validated_operation_id != operation_id
                or validated_revision != revision
            ):
                raise ValueError("task binding changed")
            confirmation = _confirmation_payload(
                snapshot,
                revision,
                state["active_operation_type"],
            )
        except (IdempotencyConflictError, ValueError):
            return {
                "operation_id": str(operation_id),
                "task_status": "unknown",
                "task_revision": revision,
                "requires_confirmation": False,
                "confirmation": None,
                "operation_status": None,
                "code": "CHAT_TASK_STATE_UNSAFE",
            }
        requires_confirmation = True

    return {
        "operation_id": str(operation_id),
        "task_status": phase.lower(),
        "task_revision": revision,
        "requires_confirmation": requires_confirmation,
        "confirmation": confirmation,
        # Redis describes conversation progress, not the MySQL business fact.
        "operation_status": None,
        "code": code_by_phase[phase],
    }


def _read_confirmed_operation(
    session_factory: SessionFactory,
    current_user: User,
    *,
    operation_id: str,
    operation_type: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Read and fingerprint-check one actor-owned operation without mutation."""
    db = session_factory()
    try:
        operation = get_operation_for_actor(db, operation_id, current_user.id)
        expected = calculate_request_fingerprint(operation_type, snapshot)
        if (
            operation.operation_type != operation_type
            or operation.fingerprint_version != FINGERPRINT_VERSION
            or operation.request_fingerprint != expected
        ):
            raise IdempotencyConflictError(
                "operation_id is bound to a different confirmed request"
            )
        safe_status = get_safe_operation_status(
            db,
            operation_id=operation_id,
            actor_user_id=current_user.id,
        )
        if safe_status["status"] == "SUCCEEDED":
            result_payload = safe_status.get("result_payload")
            application_id = (
                result_payload.get("application_id")
                if isinstance(result_payload, dict)
                else None
            )
            ownership_field = {
                MANAGER_REVIEW_OPERATION: LeaveApplication.manager_id,
                HR_REVIEW_OPERATION: LeaveApplication.hr_id,
            }.get(operation_type, LeaveApplication.employee_id)
            owned_application = (
                db.query(LeaveApplication.id)
                .filter(
                    LeaveApplication.id == application_id,
                    ownership_field == current_user.id,
                )
                .first()
            )
            if owned_application is None:
                raise OperationResultUnknownError(
                    "Succeeded operation resource ownership cannot be verified"
                )
        return safe_status
    finally:
        db.close()


def _operation_outcome_updates(
    state: ChatState,
    *,
    operation_status: str,
    result_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    replayed: bool,
) -> dict[str, Any]:
    operation_id, revision, _snapshot = _validate_task_snapshot(state)
    operation_type = state["active_operation_type"]
    intent_by_operation = {
        SUBMIT_LEAVE_OPERATION: "submit_leave",
        CANCEL_LEAVE_OPERATION: "cancel_leave",
        MANAGER_REVIEW_OPERATION: "manager_review",
        HR_REVIEW_OPERATION: "hr_review",
    }
    tool_by_operation = {
        SUBMIT_LEAVE_OPERATION: "submit_leave",
        CANCEL_LEAVE_OPERATION: "cancel_leave",
        MANAGER_REVIEW_OPERATION: "manager_review",
        HR_REVIEW_OPERATION: "hr_review",
    }
    intent = intent_by_operation[operation_type]
    tool_name = tool_by_operation[operation_type]
    if operation_status == "SUCCEEDED":
        application_id = (
            result_payload.get("application_id")
            if isinstance(result_payload, dict)
            else None
        )
        if not application_id:
            operation_status = "UNKNOWN"
        else:
            if operation_type == CANCEL_LEAVE_OPERATION:
                response = f"请假申请 {application_id} 已撤销。"
            elif operation_type == MANAGER_REVIEW_OPERATION:
                decision = state["task_payload_snapshot"]["decision"]
                response = (
                    f"请假申请 {application_id} 已通过主管审批，现等待 HR 审批。"
                    if decision == "approved"
                    else f"请假申请 {application_id} 已被主管拒绝。"
                )
            elif operation_type == HR_REVIEW_OPERATION:
                decision = state["task_payload_snapshot"]["decision"]
                response = (
                    f"请假申请 {application_id} 已通过 HR 终审。"
                    if decision == "approved"
                    else f"请假申请 {application_id} 已被 HR 终审拒绝。"
                )
            else:
                response = (
                    f"请假申请已提交成功，申请编号为 {application_id}，"
                    "当前状态为等待主管审批。"
                )
            return {
                "intent": intent,
                "task_phase": "SUCCEEDED",
                "pending_intent": None,
                "pending_fields": [],
                "current_task": None,
                "error": None,
                "code": "CHAT_OPERATION_SUCCEEDED",
                "success": True,
                "response": response,
                "tool_name": tool_name,
                "tool_result": {
                    "success": True,
                    "data": {"application_id": application_id},
                },
                "response_operation_id": operation_id,
                "response_task_status": "succeeded",
                "response_task_revision": revision,
                "requires_confirmation": False,
                "response_operation_status": "SUCCEEDED",
                "response_replayed": replayed,
            }

    if operation_status == "PROCESSING":
        phase, task_status, code, response = (
            "EXECUTING",
            "executing",
            "OPERATION_IN_PROGRESS",
            "该操作正在处理中，请查询操作状态，勿重复提交。",
        )
    elif operation_status == "FAILED":
        phase, task_status, code, response = (
            "FAILED",
            "failed",
            error_code or "OPERATION_FAILED",
            "该操作已失败，不会自动重新执行。",
        )
    else:
        operation_status = "UNKNOWN"
        phase, task_status, code, response = (
            "UNKNOWN",
            "unknown",
            "OPERATION_RESULT_UNKNOWN",
            "该操作结果暂时无法确认，请查询状态，勿重复提交。",
        )
    return {
        "intent": intent,
        "task_phase": phase,
        "error": code,
        "code": code,
        "success": False,
        "response": response,
        "tool_name": tool_name,
        "tool_result": None,
        "response_operation_id": operation_id,
        "response_task_status": task_status,
        "response_task_revision": revision,
        "requires_confirmation": False,
        "response_operation_status": operation_status,
        "response_replayed": replayed,
    }


def _resolve_existing_confirmed_operation(
    state: ChatState,
    session_factory: SessionFactory,
    current_user: User,
) -> dict[str, Any]:
    operation_id, _revision, snapshot = _validate_task_snapshot(state)
    operation_type = state["active_operation_type"]
    try:
        status = _read_confirmed_operation(
            session_factory,
            current_user,
            operation_id=operation_id,
            operation_type=operation_type,
            snapshot=snapshot,
        )
    except OperationNotFoundError:
        return _operation_outcome_updates(
            state,
            operation_status="UNKNOWN",
            replayed=True,
        )
    except IdempotencyConflictError:
        return {
            **_operation_outcome_updates(
                state,
                operation_status="FAILED",
                error_code="IDEMPOTENCY_CONFLICT",
                replayed=True,
            ),
            "response_operation_status": None,
            "response": "operation_id 与当前已确认参数不匹配。",
        }
    except Exception:
        return _operation_outcome_updates(
            state,
            operation_status="UNKNOWN",
            replayed=True,
        )
    return _operation_outcome_updates(
        state,
        operation_status=status["status"],
        result_payload=status.get("result_payload"),
        error_code=status.get("error_code"),
        replayed=True,
    )


def _execute_confirmed_submit_leave(
    state: ChatState,
    session_factory: SessionFactory,
    current_user: User,
    ip_address: str | None,
) -> dict[str, Any]:
    operation_id, _revision, snapshot = _validate_task_snapshot(
        state,
        SUBMIT_LEAVE_OPERATION,
    )
    try:
        existing = _read_confirmed_operation(
            session_factory,
            current_user,
            operation_id=operation_id,
            operation_type=SUBMIT_LEAVE_OPERATION,
            snapshot=snapshot,
        )
    except OperationNotFoundError:
        existing = None
    except IdempotencyConflictError:
        return {
            **_operation_outcome_updates(
                state,
                operation_status="FAILED",
                error_code="IDEMPOTENCY_CONFLICT",
                replayed=True,
            ),
            "response_operation_status": None,
            "response": "operation_id 已绑定到不同的业务请求。",
        }
    except Exception:
        return _operation_outcome_updates(
            state,
            operation_status="UNKNOWN",
            replayed=False,
        )

    if existing is not None:
        return _operation_outcome_updates(
            state,
            operation_status=existing["status"],
            result_payload=existing.get("result_payload"),
            error_code=existing.get("error_code"),
            replayed=True,
        )

    submit_tool = build_submit_leave_tool(
        session_factory,
        current_user,
        operation_id=operation_id,
        confirmed_payload=snapshot,
        ip_address=ip_address,
    )
    updates = _execute_confirmed_write_tool_via_toolnode(
        state,
        write_tool=submit_tool,
        expected_arguments=dict(snapshot),
        prompt=(
            "你正在执行已经由用户明确确认且由后端绑定的请假提交操作。\n"
            "必须且只能调用一次 submit_leave 工具，并严格使用已确认的"
            " leave_type、start_date、end_date 和 reason；reason 未填写时必须为 null。\n"
            f"leave_type={snapshot['leave_type']}\n"
            f"start_date={snapshot['start_date']}\n"
            f"end_date={snapshot['end_date']}\n"
            f"reason={snapshot['reason']!r}\n"
            "不要添加 current_user、user_id、manager_id、role、operation_id、"
            "thread_id、revision、fingerprint、确认令牌或数据库信息，"
            "也不要直接声称提交成功。"
        ),
        human_message="用户已通过结构化确认批准提交当前绑定的请假申请。",
        unavailable_message="请假提交服务暂时不可用，请查询操作状态。",
        invalid_message="请假提交工具调用未通过安全校验，未创建请假申请。",
    )
    if updates.get("task_phase") == "UNKNOWN":
        return _resolve_existing_confirmed_operation(
            state,
            session_factory,
            current_user,
        )
    return updates
    reason = payload.get("reason") or "未填写"
    return (
        "请确认以下请假信息：\n"
        f"类型：{leave_type}\n"
        f"日期：{payload.get('start_date')} 至 {payload.get('end_date')}\n"
        f"原因：{reason}\n"
        f"任务版本：{revision}\n"
        "确认无误后，请使用相同 operation_id、最新 task_revision，"
        "并将 task_action 设为 confirm。"
    )


def _confirm_submit_leave_node(state: ChatState) -> dict[str, Any]:
    operation_type = state.get("active_operation_type")
    required_role = {
        MANAGER_REVIEW_OPERATION: "manager",
        HR_REVIEW_OPERATION: "hr",
    }.get(operation_type, "employee")
    if state.get("user_role") != required_role:
        return _protocol_failure(
            state,
            code="permission_denied",
            response="当前账号没有确认该请假任务的权限。",
        )
    operation_id = state.get("request_operation_id")
    revision = state.get("request_task_revision")
    if (
        operation_id is None
        or operation_id != state.get("active_operation_id")
    ):
        return _protocol_failure(
            state,
            code="CHAT_OPERATION_ID_CONFLICT",
            response="确认请求与当前任务的 operation_id 不一致。",
        )
    if revision is None or revision != state.get("task_revision", 0):
        return _protocol_failure(
            state,
            code="CHAT_TASK_REVISION_CONFLICT",
            response="确认请求已过期，请使用最新 task_revision。",
        )
    phase = state.get("task_phase")
    if phase not in {
        "AWAITING_CONFIRMATION",
        "EXECUTING",
        "SUCCEEDED",
        "FAILED",
        "UNKNOWN",
    }:
        return _protocol_failure(
            state,
            code="CHAT_TASK_NOT_AWAITING_CONFIRMATION",
            response="当前任务尚未进入待确认状态。",
        )
    snapshot = state.get("task_payload_snapshot", {})
    expected = state.get("confirmation_fingerprint")
    try:
        actual = _task_fingerprint(snapshot, operation_type)
    except Exception:
        actual = None
    if (
        operation_type not in CONFIRMABLE_OPERATION_TYPES
        or not expected
        or expected != state.get("task_fingerprint")
        or actual != expected
    ):
        return _protocol_failure(
            state,
            code="CHAT_CONFIRMATION_INVALIDATED",
            response="待确认参数已经变化，请重新核对最新摘要。",
            task_status="awaiting_confirmation",
        )
    if phase == "AWAITING_CONFIRMATION":
        intent = {
            SUBMIT_LEAVE_OPERATION: "submit_leave",
            CANCEL_LEAVE_OPERATION: "cancel_leave",
            MANAGER_REVIEW_OPERATION: "manager_review",
            HR_REVIEW_OPERATION: "hr_review",
        }[operation_type]
        response = {
            SUBMIT_LEAVE_OPERATION: "确认信息有效，正在安全提交请假申请。",
            CANCEL_LEAVE_OPERATION: "确认信息有效，正在安全撤销请假申请。",
            MANAGER_REVIEW_OPERATION: "确认信息有效，正在安全执行主管审批。",
            HR_REVIEW_OPERATION: "确认信息有效，正在安全执行 HR 终审。",
        }[operation_type]
        return {
            "intent": intent,
            "task_phase": "EXECUTING",
            "error": None,
            "code": "CHAT_OPERATION_EXECUTING",
            "success": False,
            "response": response,
            "response_operation_id": operation_id,
            "response_task_status": "executing",
            "response_task_revision": revision,
            "requires_confirmation": False,
            "response_operation_status": "PROCESSING",
            "response_replayed": False,
        }
    return {
        "intent": {
            SUBMIT_LEAVE_OPERATION: "submit_leave",
            CANCEL_LEAVE_OPERATION: "cancel_leave",
            MANAGER_REVIEW_OPERATION: "manager_review",
            HR_REVIEW_OPERATION: "hr_review",
        }[operation_type],
        "error": None,
        "code": "CHAT_OPERATION_RECONCILE_REQUIRED",
        "success": False,
        "response": "正在核查已确认操作的持久化结果。",
        "response_operation_id": operation_id,
        "response_task_status": phase.lower(),
        "response_task_revision": revision,
        "requires_confirmation": False,
        "response_operation_status": (
            "PROCESSING" if phase == "EXECUTING" else phase
        ),
        "response_replayed": True,
    }


def _cancel_submit_leave_task_node(state: ChatState) -> dict[str, Any]:
    operation_type = state.get("active_operation_type")
    required_role = {
        MANAGER_REVIEW_OPERATION: "manager",
        HR_REVIEW_OPERATION: "hr",
    }.get(operation_type, "employee")
    if state.get("user_role") != required_role:
        return _protocol_failure(
            state,
            code="permission_denied",
            response="当前账号没有取消该请假任务的权限。",
        )
    active_operation_id = state.get("active_operation_id")
    if active_operation_id is None:
        return _protocol_failure(
            state,
            code="CHAT_TASK_NOT_FOUND",
            response="当前没有可取消的聊天请假任务。",
        )
    if state.get("request_operation_id") != active_operation_id:
        return _protocol_failure(
            state,
            code="CHAT_OPERATION_ID_CONFLICT",
            response="取消请求与当前任务的 operation_id 不一致。",
        )
    if state.get("request_task_revision") != state.get("task_revision", 0):
        return _protocol_failure(
            state,
            code="CHAT_TASK_REVISION_CONFLICT",
            response="取消请求已过期，请使用最新 task_revision。",
        )
    if state.get("task_phase") not in MUTABLE_TASK_PHASES:
        return _protocol_failure(
            state,
            code="CHAT_TASK_NOT_CANCELLABLE",
            response="当前任务状态不能通过聊天取消。",
        )
    cancelling_business_cancel = operation_type == CANCEL_LEAVE_OPERATION
    cancelling_manager_review = operation_type == MANAGER_REVIEW_OPERATION
    cancelling_hr_review = operation_type == HR_REVIEW_OPERATION
    return {
        **_close_submit_task(state),
        "intent": "general_chat",
        "extracted_info": {},
        "application_id": None,
        "pending_intent": None,
        "pending_fields": [],
        "current_task": None,
        "cancel_current_task": False,
        "error": None,
        "code": "CHAT_TASK_CANCELLED",
        "success": True,
        "response": (
            "已取消当前 HR 终审确认任务，没有终审请假申请。"
            if cancelling_hr_review
            else (
                "已取消当前主管审批确认任务，没有审批请假申请。"
                if cancelling_manager_review
                else (
                    "已取消当前撤销确认任务，没有撤销请假申请。"
                    if cancelling_business_cancel
                    else "已取消当前未提交的请假任务，没有创建请假申请。"
                )
            )
        ),
        "response_operation_id": active_operation_id,
        "response_task_status": "cancelled",
        "response_task_revision": state.get("task_revision", 0),
    }


def _validate_request_node(
    state: ChatState,
    context: AgentExecutionContext | None = None,
) -> dict:
    if state.get("cancel_current_task") and state.get("pending_intent"):
        return {
            **_close_submit_task(state),
            "intent": "general_chat",
            "extracted_info": {},
            "application_id": None,
            "pending_intent": None,
            "pending_fields": [],
            "current_task": None,
            "cancel_current_task": False,
            "error": "current_task_cancelled",
            "success": True,
            "response": "已取消当前未完成的操作。",
        }
    if state.get("cancel_current_task"):
        return {
            **_close_submit_task(state),
            "intent": "general_chat",
            "cancel_current_task": False,
            "error": "no_pending_task",
            "success": True,
            "response": "当前没有未完成的操作。",
        }
    if state.get("error"):
        return {}

    intent = state.get("intent", "")
    if intent not in SUPPORTED_INTENTS:
        return {
            "intent": "general_chat",
            "error": "unsupported_intent",
            "success": False,
            "response": "暂时无法识别该请求，请使用系统支持的请假相关功能。",
        }

    allowed = ALLOWED_INTENTS_BY_ROLE.get(state["user_role"], {"general_chat"})
    if intent not in allowed:
        return {
            "error": "permission_denied",
            "success": False,
            "response": "当前账号没有执行该操作的权限。",
        }

    # With no active write task, request_operation_id is only an unbound
    # client candidate.  submit_leave binds it below; every other intent must
    # ignore it so ordinary chat and read-only ToolNode queries keep working.
    # An active task is still protected by _normalize_task_state_node, which
    # requires the exact bound operation id and task revision before intent
    # recognition is allowed to proceed.

    extracted = dict(state.get("extracted_info", {}))
    application_id = extracted.get("application_id")
    reference_index = state.get("reference_index")
    if application_id in (None, "") and reference_index is not None:
        results = state.get("last_query_results", [])
        match = next(
            (item for item in results if item.get("index") == reference_index),
            None,
        )
        if match is None:
            return {
                "error": "invalid_reference_index",
                "success": False,
                "response": "请先查询相关申请，并使用申请编号或列表序号指定目标。",
            }
        application_id = match["application_id"]
        extracted["application_id"] = application_id

    missing = [
        field
        for field in REQUIRED_FIELDS.get(intent, ())
        if extracted.get(field) in (None, "")
    ]

    if intent == "submit_leave":
        request_operation_id = state.get("request_operation_id")
        active_operation_id = state.get("active_operation_id")
        if request_operation_id is None:
            return {
                **(
                    _close_submit_task(state)
                    if active_operation_id
                    else _cleared_submit_task()
                ),
                "extracted_info": {},
                "pending_intent": None,
                "pending_fields": [],
                "current_task": None,
                "error": "CHAT_OPERATION_ID_REQUIRED",
                "code": "CHAT_OPERATION_ID_REQUIRED",
                "success": False,
                "response": (
                    "聊天创建请假必须提供 operation_id 和 message_request_id，"
                    "旧客户端不能直接提交。"
                ),
            }
        if state.get("message_request_id") is None:
            return {
                **(
                    _close_submit_task(state)
                    if active_operation_id
                    else _cleared_submit_task()
                ),
                "extracted_info": {},
                "pending_intent": None,
                "pending_fields": [],
                "current_task": None,
                "error": "CHAT_MESSAGE_REQUEST_ID_REQUIRED",
                "code": "CHAT_MESSAGE_REQUEST_ID_REQUIRED",
                "success": False,
                "response": "聊天创建请假必须为每条消息提供 message_request_id。",
            }
        if (
            active_operation_id is None
            and request_operation_id in state.get("closed_operation_ids", [])
        ):
            return _protocol_failure(
                state,
                code="CHAT_OPERATION_ID_RETIRED",
                response="该 operation_id 已结束；新的独立请假任务必须使用新的 operation_id。",
            )
        if active_operation_id not in (None, request_operation_id):
            return _protocol_failure(
                state,
                code="CHAT_OPERATION_ID_CONFLICT",
                response="当前会话已有另一笔未完成任务，请先取消该任务。",
            )

        payload = _leave_task_payload(extracted)
        fingerprint = _task_fingerprint(payload)
        previous_fingerprint = state.get("task_fingerprint")
        revision = state.get("task_revision", 0)
        if active_operation_id is not None and previous_fingerprint:
            if previous_fingerprint != fingerprint:
                revision += 1

        task_updates: dict[str, Any] = {
            "active_operation_id": request_operation_id,
            "active_operation_type": SUBMIT_LEAVE_OPERATION,
            "active_task_user_id": state.get("user_id"),
            "active_task_thread_id": state.get("thread_id"),
            "task_revision": revision,
            "task_payload_snapshot": payload,
            "task_fingerprint": fingerprint,
            "confirmation_fingerprint": None,
            "response_operation_id": request_operation_id,
            "response_task_status": "collecting",
            "response_task_revision": revision,
            "requires_confirmation": False,
            "extracted_info": extracted,
            "pending_intent": intent,
            "current_task": intent,
        }
        if missing:
            labels = "、".join(MISSING_FIELD_LABELS[field] for field in missing)
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": missing,
                "error": "missing_fields",
                "success": False,
                "response": f"请补充{labels}。",
            }

        try:
            validated = LeaveCreate.model_validate(payload)
        except ValidationError:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "leave_validation_error",
                "success": False,
                "response": "请假参数格式无效，请修改后重新确认。",
            }
        if validated.start_date > validated.end_date:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "leave_validation_error",
                "success": False,
                "response": "开始日期不能晚于结束日期，请修改后重新确认。",
            }
        if validated.start_date < date.today():
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "leave_validation_error",
                "success": False,
                "response": "开始日期不能早于今天，请修改后重新确认。",
            }
        if state.get("manager_id") is None:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "leave_validation_error",
                "success": False,
                "response": "当前员工未配置直属主管，暂时无法准备请假申请。",
            }

        snapshot = validated.model_dump(mode="json")
        confirmation_fingerprint = _task_fingerprint(snapshot)
        return {
            **task_updates,
            "task_phase": "AWAITING_CONFIRMATION",
            "task_payload_snapshot": snapshot,
            "task_fingerprint": confirmation_fingerprint,
            "confirmation_fingerprint": confirmation_fingerprint,
            "pending_fields": [],
            "error": "awaiting_confirmation",
            "code": "CHAT_CONFIRMATION_REQUIRED",
            "success": False,
            "response": _confirmation_summary(snapshot, revision),
            "response_task_status": "awaiting_confirmation",
            "requires_confirmation": True,
            "response_confirmation": _confirmation_payload(snapshot, revision),
        }

    if intent == "cancel_leave":
        request_operation_id = state.get("request_operation_id")
        active_operation_id = state.get("active_operation_id")
        active_operation_type = state.get("active_operation_type")
        if active_operation_id is not None and active_operation_type != CANCEL_LEAVE_OPERATION:
            return {
                **_switch_from_submit_task(state),
                "intent": "general_chat",
                "error": "CHAT_TASK_SWITCHED",
                "success": False,
                "response": (
                    "已结束原请假任务。撤销申请属于新的写操作，"
                    "请重新发送撤销请求以生成独立 operation_id。"
                ),
            }
        if request_operation_id is None:
            return {
                "error": "CHAT_OPERATION_ID_REQUIRED",
                "code": "CHAT_OPERATION_ID_REQUIRED",
                "success": False,
                "response": "聊天撤销请假必须提供 operation_id 和 message_request_id。",
            }
        if state.get("message_request_id") is None:
            return {
                "error": "CHAT_MESSAGE_REQUEST_ID_REQUIRED",
                "code": "CHAT_MESSAGE_REQUEST_ID_REQUIRED",
                "success": False,
                "response": "聊天撤销请假必须为每条消息提供 message_request_id。",
            }
        if (
            active_operation_id is None
            and request_operation_id in state.get("closed_operation_ids", [])
        ):
            return _protocol_failure(
                state,
                code="CHAT_OPERATION_ID_RETIRED",
                response="该 operation_id 已结束；新的撤销任务必须使用新的 operation_id。",
            )
        if active_operation_id not in (None, request_operation_id):
            return _protocol_failure(
                state,
                code="CHAT_OPERATION_ID_CONFLICT",
                response="当前会话已有另一笔未完成任务，请先取消该任务。",
            )

        revision = state.get("task_revision", 0)
        task_updates: dict[str, Any] = {
            "active_operation_id": request_operation_id,
            "active_operation_type": CANCEL_LEAVE_OPERATION,
            "active_task_user_id": state.get("user_id"),
            "active_task_thread_id": state.get("thread_id"),
            "task_revision": revision,
            "task_payload_snapshot": {"application_id": application_id},
            "task_fingerprint": None,
            "confirmation_fingerprint": None,
            "response_operation_id": request_operation_id,
            "response_task_status": "collecting",
            "response_task_revision": revision,
            "requires_confirmation": False,
            "response_confirmation": None,
            "extracted_info": extracted,
            "pending_intent": intent,
            "current_task": intent,
        }
        if missing:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": missing,
                "error": "missing_fields",
                "success": False,
                "response": "请先查询相关申请，并使用申请编号或列表序号指定目标。",
            }
        try:
            normalized_application_id = str(UUID(str(application_id)))
        except (TypeError, ValueError, AttributeError):
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "invalid_application_id",
                "success": False,
                "response": "请提供有效的 UUID 申请编号。",
            }
        if context is None:
            return {
                **task_updates,
                "task_phase": "UNKNOWN",
                "pending_fields": [],
                "error": "CHAT_EXECUTION_CONTEXT_REQUIRED",
                "success": False,
                "response": "无法安全核验撤销目标，未执行任何写操作。",
            }
        try:
            application = get_cancellable_leave_application(
                context.db,
                normalized_application_id,
                context.current_user,
            )
        except LeaveNotFoundError:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "not_found",
                "success": False,
                "response": "未找到属于你的可撤销请假申请。",
            }
        except LeaveConflictError:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "conflict",
                "success": False,
                "response": "该请假申请当前不可撤销。",
            }
        except Exception:
            return {
                **task_updates,
                "task_phase": "UNKNOWN",
                "pending_fields": [],
                "error": "query_failed",
                "success": False,
                "response": "暂时无法安全核验撤销目标，未执行任何写操作。",
            }

        snapshot = {
            "application_id": normalized_application_id,
            "leave_type": application.leave_type,
            "start_date": application.start_date.isoformat(),
            "end_date": application.end_date.isoformat(),
            "status": application.status,
        }
        confirmation_fingerprint = _task_fingerprint(
            snapshot,
            CANCEL_LEAVE_OPERATION,
        )
        previous_fingerprint = state.get("task_fingerprint")
        if (
            active_operation_id is not None
            and previous_fingerprint
            and previous_fingerprint != confirmation_fingerprint
        ):
            revision += 1
        return {
            **task_updates,
            "task_phase": "AWAITING_CONFIRMATION",
            "task_revision": revision,
            "task_payload_snapshot": snapshot,
            "task_fingerprint": confirmation_fingerprint,
            "confirmation_fingerprint": confirmation_fingerprint,
            "pending_fields": [],
            "error": "awaiting_confirmation",
            "code": "CHAT_CONFIRMATION_REQUIRED",
            "success": False,
            "response": _confirmation_summary(
                snapshot,
                revision,
                CANCEL_LEAVE_OPERATION,
            ),
            "response_task_status": "awaiting_confirmation",
            "response_task_revision": revision,
            "requires_confirmation": True,
            "response_confirmation": _confirmation_payload(
                snapshot,
                revision,
                CANCEL_LEAVE_OPERATION,
            ),
        }

    if intent in {"manager_review", "hr_review"}:
        is_hr_review = intent == "hr_review"
        operation_type = (
            HR_REVIEW_OPERATION if is_hr_review else MANAGER_REVIEW_OPERATION
        )
        review_label = "HR 终审" if is_hr_review else "主管审批"
        request_operation_id = state.get("request_operation_id")
        active_operation_id = state.get("active_operation_id")
        active_operation_type = state.get("active_operation_type")
        if (
            active_operation_id is not None
            and active_operation_type != operation_type
        ):
            return {
                **_switch_from_submit_task(state),
                "intent": "general_chat",
                "error": "CHAT_TASK_SWITCHED",
                "success": False,
                "response": (
                    f"已结束原请假任务。{review_label}属于新的写操作，"
                    "请重新发送审批请求以生成独立 operation_id。"
                ),
            }
        if request_operation_id is None:
            return {
                "error": "CHAT_OPERATION_ID_REQUIRED",
                "code": "CHAT_OPERATION_ID_REQUIRED",
                "success": False,
                "response": f"聊天{review_label}必须提供 operation_id 和 message_request_id。",
            }
        if state.get("message_request_id") is None:
            return {
                "error": "CHAT_MESSAGE_REQUEST_ID_REQUIRED",
                "code": "CHAT_MESSAGE_REQUEST_ID_REQUIRED",
                "success": False,
                "response": f"聊天{review_label}必须为每条消息提供 message_request_id。",
            }
        if (
            active_operation_id is None
            and request_operation_id in state.get("closed_operation_ids", [])
        ):
            return _protocol_failure(
                state,
                code="CHAT_OPERATION_ID_RETIRED",
                response=f"该 operation_id 已结束；新的{review_label}必须使用新的 operation_id。",
            )
        if active_operation_id not in (None, request_operation_id):
            return _protocol_failure(
                state,
                code="CHAT_OPERATION_ID_CONFLICT",
                response="当前会话已有另一笔未完成任务，请先取消该任务。",
            )

        revision = state.get("task_revision", 0)
        task_updates: dict[str, Any] = {
            "active_operation_id": request_operation_id,
            "active_operation_type": operation_type,
            "active_task_user_id": state.get("user_id"),
            "active_task_thread_id": state.get("thread_id"),
            "task_revision": revision,
            "task_payload_snapshot": {
                "application_id": application_id,
                "decision": extracted.get("decision"),
                "comment": extracted.get("comment"),
            },
            "task_fingerprint": None,
            "confirmation_fingerprint": None,
            "response_operation_id": request_operation_id,
            "response_task_status": "collecting",
            "response_task_revision": revision,
            "requires_confirmation": False,
            "response_confirmation": None,
            "extracted_info": extracted,
            "pending_intent": intent,
            "current_task": intent,
        }
        if missing:
            labels = "、".join(MISSING_FIELD_LABELS[field] for field in missing)
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": missing,
                "error": "missing_fields",
                "success": False,
                "response": f"请补充{labels}。",
            }
        try:
            normalized_application_id = str(UUID(str(application_id)))
            review = LeaveReviewRequest.model_validate(
                {
                    "decision": extracted.get("decision"),
                    "comment": extracted.get("comment"),
                }
            )
        except (TypeError, ValueError, AttributeError, ValidationError):
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "invalid_hr_review" if is_hr_review else "invalid_manager_review",
                "success": False,
                "response": "请提供有效的申请编号及审批决定（approved 或 rejected）。",
            }
        if context is None:
            return {
                **task_updates,
                "task_phase": "UNKNOWN",
                "pending_fields": [],
                "error": "CHAT_EXECUTION_CONTEXT_REQUIRED",
                "success": False,
                "response": f"无法安全核验{review_label}目标，未执行任何写操作。",
            }
        try:
            application = (
                get_hr_reviewable_leave_application(
                    context.db,
                    normalized_application_id,
                    context.current_user,
                )
                if is_hr_review
                else get_manager_reviewable_leave_application(
                    context.db,
                    normalized_application_id,
                    context.current_user,
                )
            )
            employee = context.db.get(User, application.employee_id)
            if employee is None:
                raise LeaveNotFoundError("Leave application employee not found")
        except LeavePermissionError:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "permission_denied",
                "success": False,
                "response": f"当前账号没有执行{review_label}的权限。",
            }
        except LeaveNotFoundError:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "not_found",
                "success": False,
                "response": (
                    "未找到等待 HR 终审的请假申请。"
                    if is_hr_review
                    else "未找到分配给当前主管的待审批请假申请。"
                ),
            }
        except LeaveConflictError:
            return {
                **task_updates,
                "task_phase": "COLLECTING",
                "pending_fields": [],
                "error": "conflict",
                "success": False,
                "response": f"该请假申请当前不允许{review_label}。",
            }
        except Exception:
            return {
                **task_updates,
                "task_phase": "UNKNOWN",
                "pending_fields": [],
                "error": "query_failed",
                "success": False,
                "response": f"暂时无法安全核验{review_label}目标，未执行任何写操作。",
            }

        snapshot = {
            "application_id": normalized_application_id,
            "employee_name": employee.name,
            "employee_no": employee.employee_no,
            "leave_type": application.leave_type,
            "start_date": application.start_date.isoformat(),
            "end_date": application.end_date.isoformat(),
            "status": application.status,
            **review.model_dump(mode="json"),
        }
        confirmation_fingerprint = _task_fingerprint(
            snapshot,
            operation_type,
        )
        previous_fingerprint = state.get("task_fingerprint")
        if (
            active_operation_id is not None
            and previous_fingerprint
            and previous_fingerprint != confirmation_fingerprint
        ):
            revision += 1
        return {
            **task_updates,
            "task_phase": "AWAITING_CONFIRMATION",
            "task_revision": revision,
            "task_payload_snapshot": snapshot,
            "task_fingerprint": confirmation_fingerprint,
            "confirmation_fingerprint": confirmation_fingerprint,
            "pending_fields": [],
            "error": "awaiting_confirmation",
            "code": "CHAT_CONFIRMATION_REQUIRED",
            "success": False,
            "response": _confirmation_summary(
                snapshot,
                revision,
                operation_type,
            ),
            "response_task_status": "awaiting_confirmation",
            "response_task_revision": revision,
            "requires_confirmation": True,
            "response_confirmation": _confirmation_payload(
                snapshot,
                revision,
                operation_type,
            ),
        }

    if missing:
        labels = "、".join(MISSING_FIELD_LABELS[field] for field in missing)
        if intent in {"cancel_leave", "manager_review", "hr_review"} and (
            "application_id" in missing
        ):
            response = "请先查询相关申请，并使用申请编号或列表序号指定目标。"
        else:
            response = f"请补充{labels}。"
        return {
            **(
                _switch_from_submit_task(state)
                if state.get("active_operation_id")
                else {}
            ),
            "extracted_info": extracted,
            "pending_intent": intent,
            "pending_fields": missing,
            "current_task": intent,
            "error": "missing_fields",
            "success": False,
            "response": response,
        }

    if application_id is not None:
        try:
            UUID(str(application_id))
        except (TypeError, ValueError, AttributeError):
            return {
                "error": "invalid_application_id",
                "success": False,
                "response": "请提供有效的 UUID 申请编号。",
            }
    return {
        **(
            _switch_from_submit_task(state)
            if state.get("active_operation_id")
            else {}
        ),
        "extracted_info": extracted,
        "application_id": application_id,
        "pending_fields": [],
    }


def _route_intent_node(state: ChatState) -> dict:
    return {"tool_name": state.get("intent")}


def _after_validation(state: ChatState) -> str:
    return "generate_response" if state.get("error") else "route_intent"


def _select_intent(state: ChatState) -> str:
    return state["intent"]


def _format_leave_lines(items: list[dict[str, Any]], include_employee: bool) -> str:
    lines = []
    for index, item in enumerate(items, start=1):
        employee_text = ""
        if include_employee and item.get("employee"):
            employee = item["employee"]
            employee_text = f"，员工：{employee['name']}（{employee['employee_no']}）"
        lines.append(
            f"{index}. 申请编号：{item['application_id']}{employee_text}，"
            f"类型：{LEAVE_TYPE_LABELS.get(item['leave_type'], item['leave_type'])}，"
            f"日期：{item['start_date']} 至 {item['end_date']}，"
            f"天数：{item['leave_days']:.1f}，状态：{item['status']}，"
            f"主管状态：{item['manager_status']}，HR状态：{item['hr_status']}"
        )
    return "\n".join(lines)


def _query_references(result: dict[str, Any]) -> list[dict[str, Any]]:
    if not result.get("success") or not isinstance(result.get("data"), list):
        return []
    return [
        {
            "index": index,
            "application_id": item["application_id"],
            "summary": (
                f"{item.get('start_date')}至{item.get('end_date')} "
                f"{item.get('leave_type')} {item.get('status')}"
            ),
        }
        for index, item in enumerate(result["data"][:20], start=1)
    ]


def _invalid_query_tool_call(reason: str) -> dict[str, Any]:
    return {
        "error": reason,
        "success": False,
        "response": "暂时无法安全执行该操作，请重新尝试。",
        "tool_result": None,
    }


def _query_tool_arguments_are_valid(tool: BaseTool, arguments: object) -> bool:
    if not isinstance(arguments, dict):
        return False
    schema = tool.get_input_schema()
    properties = schema.model_json_schema().get("properties", {})
    if set(arguments) - set(properties):
        return False
    try:
        schema.model_validate(arguments)
    except ValidationError:
        return False
    return True


def _validate_query_tool_call_node(
    state: ChatState,
    expected_tool: BaseTool | None = None,
    expected_arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    messages = state.get("messages", [])
    if not messages or not isinstance(messages[-1], AIMessage):
        return _invalid_query_tool_call("invalid_tool_call_message")

    message = messages[-1]
    if message.invalid_tool_calls:
        return _invalid_query_tool_call("invalid_tool_call_schema")
    if len(message.tool_calls) != 1:
        return _invalid_query_tool_call("invalid_tool_call_count")

    call = message.tool_calls[0]
    expected_tool_name = (
        expected_tool.name if expected_tool is not None else QUERY_MY_LEAVE_TOOL_NAME
    )
    if call.get("name") != expected_tool_name:
        return _invalid_query_tool_call("unknown_tool_call")
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        return _invalid_query_tool_call("missing_tool_call_id")
    arguments = call.get("args")
    if expected_tool is None:
        arguments_are_valid = arguments == {}
    else:
        arguments_are_valid = _query_tool_arguments_are_valid(
            expected_tool,
            arguments,
        )
    if not arguments_are_valid:
        return _invalid_query_tool_call("invalid_tool_call_arguments")
    if expected_arguments is not None and arguments != expected_arguments:
        return _invalid_query_tool_call("tool_call_confirmation_mismatch")
    return {"error": None, "response": ""}


def _after_query_tool_validation(state: ChatState) -> str:
    return "generate_response" if state.get("error") else "execute_tool"


def _collect_query_tool_result_node(
    state: ChatState,
    expected_tool: BaseTool | None = None,
) -> dict[str, Any]:
    messages = state.get("messages", [])
    if not messages or not isinstance(messages[-1], ToolMessage):
        return _invalid_query_tool_call("missing_tool_message")

    tool_message = messages[-1]
    call_message = messages[-2] if len(messages) >= 2 else None
    if not isinstance(call_message, AIMessage) or len(call_message.tool_calls) != 1:
        return _invalid_query_tool_call("missing_tool_call_message")

    call = call_message.tool_calls[0]
    expected_tool_name = (
        expected_tool.name if expected_tool is not None else QUERY_MY_LEAVE_TOOL_NAME
    )
    if (
        call.get("name") != expected_tool_name
        or tool_message.name != expected_tool_name
        or tool_message.tool_call_id != call.get("id")
    ):
        return _invalid_query_tool_call("tool_call_id_mismatch")
    if not isinstance(tool_message.artifact, dict):
        return {
            "tool_result": {
                "success": False,
                "error_type": "query_failed",
                "message": "查询服务暂时不可用，请稍后重试。",
            }
        }

    result = tool_message.artifact
    return {
        "tool_name": f"{expected_tool_name}_tool",
        "tool_result": result,
        "last_query_results": _query_references(result),
    }


def _generate_response_node(state: ChatState) -> dict:
    response = state.get("response", "")
    success = state.get("success", False)
    error = state.get("error")
    if not response and state.get("intent") == "submit_leave":
        # Phase 2A-4B has no business execution path. Never infer success from a
        # missing result or a future routing mistake.
        success = False
        error = "CHAT_OPERATION_NOT_YET_EXECUTABLE"
        response = "当前阶段仅准备确认信息，未创建请假申请。"
    if not response:
        intent = state["intent"]
        result = state.get("tool_result") or {"success": True, "data": None}
        if not result.get("success", False):
            success = False
            error = result.get("error_type", "tool_error")
            response = f"操作失败：{result.get('message', '未知错误')}。"
        else:
            success = True
            data = result.get("data")
            if intent == "submit_leave":
                response = (
                    f"请假申请已提交成功，申请编号为 {data['application_id']}，"
                    "当前状态为等待主管审批。"
                )
            elif intent == "cancel_leave":
                response = f"请假申请 {data['application_id']} 已撤销。"
            elif intent == "manager_review":
                response = (
                    f"主管审批已完成，申请 {data['application_id']} 的主管状态为 "
                    f"{data['manager_status']}。"
                )
            elif intent == "hr_review":
                response = (
                    f"HR审批已完成，申请 {data['application_id']} 的最终状态为 "
                    f"{data['status']}。"
                )
            elif intent == "query_my_leave":
                response = (
                    "当前没有请假记录。"
                    if not data
                    else "你的请假记录如下：\n" + _format_leave_lines(data, False)
                )
            elif intent == "query_manager_pending":
                response = (
                    "当前没有待审批申请。"
                    if not data
                    else "待你审批的申请如下：\n" + _format_leave_lines(data, True)
                )
            elif intent == "query_hr_pending":
                response = (
                    "当前没有待 HR 审批的申请。"
                    if not data
                    else "待 HR 审批的申请如下：\n" + _format_leave_lines(data, True)
                )
            else:
                response = (
                    "你好，我是智能 OA 请假助手。我可以帮助员工提交、查询和撤销请假，"
                    "帮助主管查询及审批申请，也可以帮助 HR 查询和审批进入 HR 环节的申请。"
                )

    updates: dict[str, Any] = {
        "success": success,
        "error": error,
        "response": response,
        "messages": [AIMessage(content=response)],
    }
    result = state.get("tool_result") or {}
    if success and state.get("intent") != "general_chat" and result.get("success"):
        updates.update(
            {
                "pending_intent": None,
                "pending_fields": [],
                "current_task": None,
                "extracted_info": {},
            }
        )
    return updates


def build_chat_graph(
    context: AgentExecutionContext,
    recognizer: Callable[[ChatState], dict] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """Build a request-scoped graph using the shared Redis checkpointer."""
    recognize_node = recognizer or recognize_intent
    query_tools: dict[str, tuple[BaseTool, str]] = {
        "query_my_leave": (
            build_query_my_leave_tool(context.db, context.current_user),
            QUERY_MY_LEAVE_TOOL_PROMPT,
        ),
        "query_manager_pending": (
            build_query_manager_pending_tool(context.db, context.current_user),
            QUERY_MANAGER_PENDING_TOOL_PROMPT,
        ),
        "query_hr_pending": (
            build_query_hr_pending_tool(context.db, context.current_user),
            QUERY_HR_PENDING_TOOL_PROMPT,
        ),
    }

    def submit_leave_node(state: ChatState) -> dict:
        # Defense in depth: validation must always turn submit_leave into either
        # collection or confirmation preparation. Even a future routing mistake
        # cannot call the legacy committing service from this graph node.
        return _protocol_failure(
            state,
            code="CHAT_CONFIRMATION_REQUIRED",
            response="请先完成参数收集并确认；当前阶段不会创建请假申请。",
            task_status=state.get("task_phase", "COLLECTING").lower(),
        )

    def make_query_tool_call_node(tool: BaseTool, prompt: str):
        def query_tool_call_node(state: ChatState) -> dict:
            try:
                tool_calling_llm = get_llm().bind_tools([tool])
                message = tool_calling_llm.invoke(
                    [
                        SystemMessage(content=prompt),
                        HumanMessage(content=state["user_message"]),
                    ]
                )
            except LLMConfigurationError:
                raise
            except Exception as exc:
                raise LLMUnavailableError("LLM tool-calling request failed") from exc
            if not isinstance(message, AIMessage):
                return _invalid_query_tool_call("invalid_tool_call_message")
            message.additional_kwargs["smart_oa_internal_tool_call"] = True
            return {
                "messages": [message],
                "tool_name": tool.name,
            }

        return query_tool_call_node

    def cancel_leave_node(state: ChatState) -> dict:
        return _protocol_failure(
            state,
            code="CHAT_CONFIRMATION_REQUIRED",
            response="撤销请假必须先完成目标核验并由用户明确确认。",
            task_status=state.get("task_phase", "COLLECTING").lower(),
        )

    def manager_review_node(state: ChatState) -> dict:
        return _protocol_failure(
            state,
            code="CHAT_CONFIRMATION_REQUIRED",
            response="主管审批必须先完成目标核验并由用户明确确认。",
            task_status=state.get("task_phase", "COLLECTING").lower(),
        )

    def hr_review_node(state: ChatState) -> dict:
        return _protocol_failure(
            state,
            code="CHAT_CONFIRMATION_REQUIRED",
            response="HR 终审必须先完成目标核验并由用户明确确认。",
            task_status=state.get("task_phase", "COLLECTING").lower(),
        )

    def general_chat_node(state: ChatState) -> dict:
        return {"tool_name": None, "tool_result": {"success": True, "data": None}}

    graph = StateGraph(ChatState)
    graph.add_node("normalize_task_state", _normalize_task_state_node)
    graph.add_node("confirm_submit_leave", _confirm_submit_leave_node)
    graph.add_node("cancel_submit_leave_task", _cancel_submit_leave_task_node)
    graph.add_node("recognize_intent", recognize_node)
    graph.add_node(
        "validate_request",
        lambda state: _validate_request_node(state, context),
    )
    graph.add_node("route_intent", _route_intent_node)
    graph.add_node("submit_leave", submit_leave_node)
    for intent, (tool, prompt) in query_tools.items():
        graph.add_node(intent, make_query_tool_call_node(tool, prompt))
        graph.add_node(
            f"validate_{intent}_tool_call",
            lambda state, expected_tool=tool: _validate_query_tool_call_node(
                state,
                expected_tool,
            ),
        )
        graph.add_node(
            f"{intent}_tool",
            ToolNode(
                [tool],
                handle_tool_errors="查询服务暂时不可用，请稍后重试。",
            ),
        )
        graph.add_node(
            f"collect_{intent}_tool_result",
            lambda state, expected_tool=tool: _collect_query_tool_result_node(
                state,
                expected_tool,
            ),
        )
    graph.add_node("cancel_leave", cancel_leave_node)
    graph.add_node("manager_review", manager_review_node)
    graph.add_node("hr_review", hr_review_node)
    graph.add_node("general_chat", general_chat_node)
    graph.add_node("generate_response", _generate_response_node)
    graph.add_edge(START, "normalize_task_state")
    graph.add_conditional_edges(
        "normalize_task_state",
        _after_task_protocol,
        {
            "recognize_intent": "recognize_intent",
            "confirm_submit_leave": "confirm_submit_leave",
            "cancel_submit_leave_task": "cancel_submit_leave_task",
            "generate_response": "generate_response",
        },
    )
    graph.add_edge("confirm_submit_leave", "generate_response")
    graph.add_edge("cancel_submit_leave_task", "generate_response")
    graph.add_edge("recognize_intent", "validate_request")
    graph.add_conditional_edges(
        "validate_request",
        _after_validation,
        {"route_intent": "route_intent", "generate_response": "generate_response"},
    )
    graph.add_conditional_edges(
        "route_intent", _select_intent, {intent: intent for intent in SUPPORTED_INTENTS}
    )
    for intent in SUPPORTED_INTENTS - READ_ONLY_QUERY_INTENTS:
        graph.add_edge(intent, "generate_response")
    for intent in READ_ONLY_QUERY_INTENTS:
        validate_node = f"validate_{intent}_tool_call"
        tool_node = f"{intent}_tool"
        collect_node = f"collect_{intent}_tool_result"
        graph.add_edge(intent, validate_node)
        graph.add_conditional_edges(
            validate_node,
            _after_query_tool_validation,
            {
                "execute_tool": tool_node,
                "generate_response": "generate_response",
            },
        )
        graph.add_edge(tool_node, collect_node)
        graph.add_edge(collect_node, "generate_response")
    graph.add_edge("generate_response", END)
    return graph.compile(checkpointer=checkpointer or get_redis_checkpointer())


def _publish_task_updates(
    base_checkpointer: BaseCheckpointSaver,
    turn_lock: ChatTurnLock | None,
    thread_id: str,
    updates: dict[str, Any],
) -> ChatState:
    """Publish one state-only transition without appending another user message."""
    update_graph = StateGraph(ChatState)
    update_graph.add_node("apply_task_update", lambda _state: updates)
    update_graph.add_edge(START, "apply_task_update")
    update_graph.add_edge("apply_task_update", END)
    buffered = (
        _TurnBufferedCheckpointSaver(base_checkpointer, turn_lock)
        if turn_lock is not None
        else None
    )
    graph = update_graph.compile(checkpointer=buffered or base_checkpointer)
    result = graph.invoke({}, config={"configurable": {"thread_id": thread_id}})
    if buffered is not None:
        buffered.flush()
    return result


def _execute_confirmed_write_tool_via_toolnode(
    state: ChatState,
    *,
    write_tool: BaseTool,
    expected_arguments: dict[str, Any],
    prompt: str,
    human_message: str,
    unavailable_message: str,
    invalid_message: str,
) -> ChatState:
    """Execute one durably confirmed write through a strictly validated ToolNode."""
    operation_id, _revision, snapshot = _validate_task_snapshot(state)
    operation_type = state["active_operation_type"]
    intent = {
        SUBMIT_LEAVE_OPERATION: "submit_leave",
        CANCEL_LEAVE_OPERATION: "cancel_leave",
        MANAGER_REVIEW_OPERATION: "manager_review",
        HR_REVIEW_OPERATION: "hr_review",
    }[operation_type]

    def request_tool_call(tool_state: ChatState) -> dict[str, Any]:
        try:
            message = get_llm().bind_tools([write_tool]).invoke(
                [
                    SystemMessage(content=prompt),
                    HumanMessage(content=human_message),
                ]
            )
        except LLMConfigurationError:
            raise
        except Exception as exc:
            raise LLMUnavailableError("LLM write tool-calling request failed") from exc
        if not isinstance(message, AIMessage):
            return _invalid_query_tool_call("invalid_tool_call_message")
        message.additional_kwargs["smart_oa_internal_tool_call"] = True
        return {"messages": [message], "tool_name": write_tool.name}

    def validate_tool_call(tool_state: ChatState) -> dict[str, Any]:
        return _validate_query_tool_call_node(
            tool_state,
            write_tool,
            expected_arguments,
        )

    def collect_tool_result(tool_state: ChatState) -> dict[str, Any]:
        return _collect_query_tool_result_node(tool_state, write_tool)

    def finalize(tool_state: ChatState) -> dict[str, Any]:
        if tool_state.get("error"):
            response = tool_state.get("response") or invalid_message
            return {
                "intent": intent,
                "task_phase": "FAILED",
                "pending_intent": None,
                "pending_fields": [],
                "current_task": None,
                "error": tool_state["error"],
                "code": tool_state["error"],
                "success": False,
                "response": response,
                "tool_result": None,
                "response_operation_id": operation_id,
                "response_task_status": "failed",
                "response_task_revision": state.get("task_revision", 0),
                "requires_confirmation": False,
                "response_operation_status": None,
                "response_replayed": False,
                "messages": [AIMessage(content=response)],
            }

        result = tool_state.get("tool_result") or {}
        operation_status = result.get("operation_status")
        if result.get("success"):
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            application_id = data.get("application_id")
            if (
                operation_type == SUBMIT_LEAVE_OPERATION
                and not application_id
            ) or (
                operation_type != SUBMIT_LEAVE_OPERATION
                and application_id != snapshot["application_id"]
            ):
                operation_status = "UNKNOWN"
            updates = _operation_outcome_updates(
                tool_state,
                operation_status=operation_status or "UNKNOWN",
                result_payload={"application_id": application_id},
                replayed=bool(result.get("replayed")),
            )
        elif operation_status in {"PROCESSING", "FAILED", "UNKNOWN"}:
            updates = _operation_outcome_updates(
                tool_state,
                operation_status=operation_status,
                error_code=str(result.get("error_type") or "OPERATION_FAILED").upper(),
                replayed=operation_status != "UNKNOWN",
            )
        else:
            response = result.get("message") or invalid_message
            updates = {
                "intent": intent,
                "task_phase": "FAILED",
                "pending_intent": None,
                "pending_fields": [],
                "current_task": None,
                "error": result.get("error_type", "tool_error"),
                "code": result.get("error_type", "tool_error"),
                "success": False,
                "response": response,
                "tool_result": result,
                "response_operation_id": operation_id,
                "response_task_status": "failed",
                "response_task_revision": state.get("task_revision", 0),
                "requires_confirmation": False,
                "response_operation_status": None,
                "response_replayed": False,
            }
        return {**updates, "messages": [AIMessage(content=updates["response"])]}

    graph = StateGraph(ChatState)
    graph.add_node("request_write_tool_call", request_tool_call)
    graph.add_node("validate_write_tool_call", validate_tool_call)
    graph.add_node(
        "confirmed_write_tool",
        ToolNode(
            [write_tool],
            handle_tool_errors=unavailable_message,
        ),
    )
    graph.add_node("collect_write_tool_result", collect_tool_result)
    graph.add_node("finalize_write_tool_result", finalize)
    graph.add_edge(START, "request_write_tool_call")
    graph.add_edge("request_write_tool_call", "validate_write_tool_call")
    graph.add_conditional_edges(
        "validate_write_tool_call",
        _after_query_tool_validation,
        {
            "execute_tool": "confirmed_write_tool",
            "generate_response": "finalize_write_tool_result",
        },
    )
    graph.add_edge("confirmed_write_tool", "collect_write_tool_result")
    graph.add_edge("collect_write_tool_result", "finalize_write_tool_result")
    graph.add_edge("finalize_write_tool_result", END)
    isolated_input = {
        **state,
        "messages": [],
        "tool_name": None,
        "tool_result": None,
        "error": None,
        "response": "",
    }
    return graph.compile().invoke(isolated_input)


def _execute_confirmed_cancel_leave_via_toolnode(
    state: ChatState,
    session_factory: SessionFactory,
    current_user: User,
    ip_address: str | None,
) -> ChatState:
    """Execute a durably confirmed cancellation through one isolated ToolNode."""
    operation_id, _revision, snapshot = _validate_task_snapshot(
        state,
        CANCEL_LEAVE_OPERATION,
    )
    cancel_tool = build_cancel_leave_tool(
        session_factory,
        current_user,
        operation_id=operation_id,
        confirmed_payload=snapshot,
        ip_address=ip_address,
    )
    return _execute_confirmed_write_tool_via_toolnode(
        state,
        write_tool=cancel_tool,
        expected_arguments={"application_id": snapshot["application_id"]},
        prompt=(
            "你正在执行已经由用户明确确认且由后端绑定的撤销请假操作。\n"
            "必须且只能调用一次 cancel_leave 工具，并使用以下唯一参数：\n"
            f'{{"application_id":"{snapshot["application_id"]}"}}\n'
            "不要添加 user_id、role、operation_id、确认令牌、数据库信息或其他参数，"
            "不要调用其他工具，也不要直接声称撤销成功。"
        ),
        human_message="用户已明确确认撤销当前绑定的请假申请。",
        unavailable_message="撤销服务暂时不可用，请查询操作状态。",
        invalid_message="撤销工具调用未通过安全校验，未执行撤销。",
    )


def _execute_confirmed_manager_review_via_toolnode(
    state: ChatState,
    session_factory: SessionFactory,
    current_user: User,
    ip_address: str | None,
) -> ChatState:
    """Execute one durably confirmed manager decision through ToolNode."""
    operation_id, _revision, snapshot = _validate_task_snapshot(
        state,
        MANAGER_REVIEW_OPERATION,
    )
    expected_arguments = {
        "application_id": snapshot["application_id"],
        "decision": snapshot["decision"],
        "comment": snapshot["comment"],
    }
    manager_tool = build_manager_review_tool(
        session_factory,
        current_user,
        operation_id=operation_id,
        confirmed_payload=snapshot,
        ip_address=ip_address,
    )
    return _execute_confirmed_write_tool_via_toolnode(
        state,
        write_tool=manager_tool,
        expected_arguments=expected_arguments,
        prompt=(
            "你正在执行已经由用户明确确认且由后端绑定的主管审批操作。\n"
            "必须且只能调用一次 manager_review 工具，并严格使用已确认的"
            " application_id、decision 和 comment；comment 未填写时必须为 null。\n"
            f"application_id={snapshot['application_id']}\n"
            f"decision={snapshot['decision']}\n"
            f"comment={snapshot['comment']!r}\n"
            "不要添加 manager_id、user_id、role、operation_id、确认令牌、"
            "数据库信息或其他参数，也不要直接声称审批成功。"
        ),
        human_message="用户已通过结构化确认批准执行当前绑定的主管审批决定。",
        unavailable_message="主管审批服务暂时不可用，请查询操作状态。",
        invalid_message="主管审批工具调用未通过安全校验，未执行审批。",
    )


def _execute_confirmed_hr_review_via_toolnode(
    state: ChatState,
    session_factory: SessionFactory,
    current_user: User,
    ip_address: str | None,
) -> ChatState:
    """Execute one durably confirmed HR final decision through ToolNode."""
    operation_id, _revision, snapshot = _validate_task_snapshot(
        state,
        HR_REVIEW_OPERATION,
    )
    expected_arguments = {
        "application_id": snapshot["application_id"],
        "decision": snapshot["decision"],
        "comment": snapshot["comment"],
    }
    hr_tool = build_hr_review_tool(
        session_factory,
        current_user,
        operation_id=operation_id,
        confirmed_payload=snapshot,
        ip_address=ip_address,
    )
    return _execute_confirmed_write_tool_via_toolnode(
        state,
        write_tool=hr_tool,
        expected_arguments=expected_arguments,
        prompt=(
            "你正在执行已经由用户明确确认且由后端绑定的 HR 终审操作。\n"
            "必须且只能调用一次 hr_review 工具，并严格使用已确认的"
            " application_id、decision 和 comment；comment 未填写时必须为 null。\n"
            f"application_id={snapshot['application_id']}\n"
            f"decision={snapshot['decision']}\n"
            f"comment={snapshot['comment']!r}\n"
            "不要添加 hr_id、user_id、role、operation_id、确认令牌、"
            "数据库信息或其他参数，也不要直接声称终审成功。"
        ),
        human_message="用户已通过结构化确认批准执行当前绑定的 HR 终审决定。",
        unavailable_message="HR 终审服务暂时不可用，请查询操作状态。",
        invalid_message="HR 终审工具调用未通过安全校验，未执行终审。",
    )


def recover_chat_confirm_operation(
    *,
    session_factory: SessionFactory,
    current_user: User,
    thread_id: str,
    operation_id: str,
    task_revision: int,
    turn_lock: ChatTurnLock,
) -> ChatState:
    """Rebuild a confirm response from MySQL without executing a mutation."""
    turn_lock.ensure_owned()
    checkpointer = get_redis_checkpointer()
    item = checkpointer.get_tuple(
        {"configurable": {"thread_id": thread_id}}
    )
    if item is None:
        raise OperationResultUnknownError("Chat checkpoint is unavailable")
    state = item.checkpoint.get("channel_values", {})
    if (
        state.get("active_task_user_id") != current_user.id
        or state.get("active_task_thread_id") != thread_id
        or state.get("active_operation_id") != operation_id
        or state.get("task_revision") != task_revision
        or state.get("task_phase")
        not in {
            "AWAITING_CONFIRMATION",
            "EXECUTING",
            "SUCCEEDED",
            "FAILED",
            "UNKNOWN",
        }
    ):
        raise OperationResultUnknownError(
            "Chat confirmation state cannot be safely correlated"
        )
    try:
        _validate_task_snapshot(state)
    except Exception as exc:
        raise OperationResultUnknownError(
            "Chat confirmation snapshot cannot be verified"
        ) from exc
    updates = _resolve_existing_confirmed_operation(
        state,
        session_factory,
        current_user,
    )
    return _publish_task_updates(
        checkpointer,
        turn_lock,
        thread_id,
        updates,
    )


def run_chat_agent(
    db: Session,
    current_user: User,
    thread_id: str,
    user_message: str,
    ip_address: str | None = None,
    recognizer: Callable[[ChatState], dict] | None = None,
    message_request_id: str | None = None,
    operation_id: str | None = None,
    task_action: str = "message",
    task_revision: int | None = None,
    turn_lock: ChatTurnLock | None = None,
    idempotency_session_factory: SessionFactory | None = None,
) -> ChatState:
    context = AgentExecutionContext(
        db=db, current_user=current_user, ip_address=ip_address
    )
    base_checkpointer = get_redis_checkpointer()
    buffered_checkpointer = (
        _TurnBufferedCheckpointSaver(base_checkpointer, turn_lock)
        if turn_lock is not None
        else None
    )
    graph = build_chat_graph(
        context,
        recognizer=recognizer,
        checkpointer=buffered_checkpointer or base_checkpointer,
    )
    turn_input: ChatState = {
        "messages": [HumanMessage(content=user_message)],
        "user_message": user_message,
        "thread_id": thread_id,
        "message_request_id": message_request_id,
        "request_operation_id": operation_id,
        "request_task_action": task_action,
        "request_task_revision": task_revision,
        "user_id": current_user.id,
        "user_role": current_user.role,
        "user_department": current_user.department,
        "manager_id": current_user.manager_id,
        "application_id": None,
        "tool_name": None,
        "tool_result": None,
        "reference_index": None,
        "cancel_current_task": False,
        "error": None,
        "success": False,
        "response": "",
        "response_operation_id": None,
        "response_task_status": None,
        "response_task_revision": None,
        "requires_confirmation": None,
        "response_operation_status": None,
        "response_replayed": None,
        "response_confirmation": None,
        "code": None,
    }
    result = graph.invoke(
        turn_input, config={"configurable": {"thread_id": thread_id}}
    )
    if buffered_checkpointer is not None:
        buffered_checkpointer.flush()
    if task_action != "confirm" or result.get("code") not in {
        "CHAT_OPERATION_EXECUTING",
        "CHAT_OPERATION_RECONCILE_REQUIRED",
    }:
        return result

    # The first flush above is the durable execution-intent barrier. No MySQL
    # mutation is permitted until that EXECUTING checkpoint has been published
    # while the exact Redis lock owner is still current.
    if turn_lock is None or idempotency_session_factory is None:
        return _publish_task_updates(
            base_checkpointer,
            turn_lock,
            thread_id,
            _operation_outcome_updates(
                result,
                operation_status="UNKNOWN",
                replayed=False,
            ),
        )
    turn_lock.ensure_owned()
    if result.get("code") == "CHAT_OPERATION_EXECUTING":
        if result.get("active_operation_type") == CANCEL_LEAVE_OPERATION:
            updates = _execute_confirmed_cancel_leave_via_toolnode(
                result,
                idempotency_session_factory,
                current_user,
                ip_address,
            )
        elif result.get("active_operation_type") == MANAGER_REVIEW_OPERATION:
            updates = _execute_confirmed_manager_review_via_toolnode(
                result,
                idempotency_session_factory,
                current_user,
                ip_address,
            )
        elif result.get("active_operation_type") == HR_REVIEW_OPERATION:
            updates = _execute_confirmed_hr_review_via_toolnode(
                result,
                idempotency_session_factory,
                current_user,
                ip_address,
            )
        else:
            updates = _execute_confirmed_submit_leave(
                result,
                idempotency_session_factory,
                current_user,
                ip_address,
            )
    else:
        updates = _resolve_existing_confirmed_operation(
            result,
            idempotency_session_factory,
            current_user,
        )
    return _publish_task_updates(
        base_checkpointer,
        turn_lock,
        thread_id,
        updates,
    )
