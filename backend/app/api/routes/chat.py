"""Authenticated chat and Redis-backed session endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.agents.chat_agent import (
    project_chat_task_state,
    recover_chat_confirm_operation,
    run_chat_agent,
)
from app.core.dependencies import get_current_business_user
from app.core.exceptions import AppException
from app.db.mysql import SessionLocal, get_db
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.session import (
    SessionCreateResponse,
    SessionListResponse,
    SessionMessagesResponse,
    SessionSummary,
    SessionTaskResponse,
)
from app.services.idempotency_service import IdempotencyRecordError
from app.services.leave_service import SessionFactory
from app.services.session_service import (
    ChatTurnInProgressError,
    ChatTurnLockLostError,
    MessageRequestConflictError,
    MessageRequestResultUnknownError,
    acquire_chat_turn_lock,
    begin_message_request,
    calculate_message_request_fingerprint,
    complete_message_request,
    SessionNotFoundError,
    create_session,
    delete_session,
    get_session_messages,
    get_session_task_checkpoint,
    get_session,
    list_sessions,
    touch_session,
    validate_session_owner,
)


router = APIRouter()


def get_chat_idempotency_session_factory() -> SessionFactory:
    """Provide fresh internal Sessions for the Chat idempotent write path."""
    return SessionLocal


def _validate_chat_protocol_envelope(payload: ChatRequest) -> None:
    """Reject protocol actions that cannot be safely correlated for replay."""
    if (
        payload.task_action != "message" or payload.operation_id is not None
    ) and payload.message_request_id is None:
        raise AppException(
            "CHAT_MESSAGE_REQUEST_ID_REQUIRED",
            "聊天任务协议必须提供 message_request_id。",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )


def _chat_response(
    payload: ChatRequest,
    result: dict,
    *,
    replayed: bool | None = None,
) -> ChatResponse:
    tool_result = result.get("tool_result") or {}
    operation_id = result.get("response_operation_id")
    return ChatResponse(
        success=result.get("success", False),
        thread_id=payload.thread_id,
        intent=result.get("intent", "general_chat"),
        response=result.get("response", "暂时无法处理该请求。"),
        data=tool_result.get("data"),
        message_request_id=payload.message_request_id,
        operation_id=operation_id,
        task_status=result.get("response_task_status"),
        task_revision=(
            result.get("response_task_revision", result.get("task_revision"))
            if operation_id is not None
            else None
        ),
        requires_confirmation=result.get("requires_confirmation"),
        operation_status=result.get("response_operation_status"),
        code=result.get("code"),
        replayed=(
            result.get("response_replayed")
            if result.get("response_replayed") is not None
            else replayed
        ),
        confirmation=result.get("response_confirmation"),
    )


def _session_summary(metadata: dict[str, str]) -> SessionSummary:
    return SessionSummary.model_validate(metadata)


@router.post(
    "/sessions",
    response_model=SessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_chat_session(
    current_user: User = Depends(get_current_business_user),
) -> SessionCreateResponse:
    metadata = create_session(current_user.id)
    return SessionCreateResponse.model_validate(metadata)


@router.get("/sessions", response_model=SessionListResponse)
def get_chat_sessions(
    current_user: User = Depends(get_current_business_user),
) -> SessionListResponse:
    sessions = list_sessions(current_user.id)
    return SessionListResponse(
        sessions=[_session_summary(metadata) for metadata in sessions]
    )


@router.get("/sessions/{thread_id}", response_model=SessionSummary)
def get_chat_session(
    thread_id: UUID,
    current_user: User = Depends(get_current_business_user),
) -> SessionSummary:
    metadata = get_session(current_user.id, thread_id)
    return _session_summary(metadata)


@router.get(
    "/sessions/{thread_id}/messages",
    response_model=SessionMessagesResponse,
)
def get_chat_session_messages(
    thread_id: UUID,
    current_user: User = Depends(get_current_business_user),
) -> SessionMessagesResponse:
    messages = get_session_messages(current_user.id, thread_id)
    return SessionMessagesResponse(thread_id=thread_id, messages=messages)


@router.get(
    "/sessions/{thread_id}/task",
    response_model=SessionTaskResponse,
)
def get_chat_session_task(
    thread_id: UUID,
    current_user: User = Depends(get_current_business_user),
) -> SessionTaskResponse:
    """Read a safe task projection; never execute the graph or business writes."""
    state = get_session_task_checkpoint(current_user.id, thread_id)
    projection = project_chat_task_state(
        state,
        user_id=current_user.id,
        thread_id=str(thread_id),
    )
    return SessionTaskResponse.model_validate(
        {"thread_id": thread_id, **projection}
    )


@router.delete("/sessions/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_session(
    thread_id: UUID,
    current_user: User = Depends(get_current_business_user),
) -> Response:
    delete_session(current_user.id, thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_business_user),
    idempotency_session_factory: SessionFactory = Depends(
        get_chat_idempotency_session_factory
    ),
) -> ChatResponse:
    thread_id = str(payload.thread_id)
    validate_session_owner(current_user.id, thread_id)
    _validate_chat_protocol_envelope(payload)
    request_fingerprint = None
    if payload.message_request_id is not None:
        request_fingerprint = calculate_message_request_fingerprint(
            message=payload.message,
            operation_id=payload.operation_id,
            task_action=payload.task_action,
            task_revision=payload.task_revision,
        )

    try:
        lock = acquire_chat_turn_lock(current_user.id, thread_id)
    except ChatTurnInProgressError as exc:
        raise AppException(
            "CHAT_TURN_IN_PROGRESS",
            "当前会话正在处理另一条消息，请稍后重试。",
            status.HTTP_409_CONFLICT,
        ) from exc

    with lock:
        if payload.message_request_id is not None and request_fingerprint is not None:
            try:
                receipt = begin_message_request(
                    current_user.id,
                    thread_id,
                    payload.message_request_id,
                    fingerprint=request_fingerprint,
                )
            except MessageRequestConflictError as exc:
                raise AppException(
                    "CHAT_MESSAGE_REQUEST_CONFLICT",
                    "message_request_id 已绑定到不同的消息内容。",
                    status.HTTP_409_CONFLICT,
                ) from exc
            except MessageRequestResultUnknownError as exc:
                if (
                    payload.task_action != "confirm"
                    or payload.operation_id is None
                    or payload.task_revision is None
                ):
                    raise AppException(
                        "CHAT_MESSAGE_RESULT_UNKNOWN",
                        "该消息此前已开始处理，但无法确认完整响应；不会自动重复执行。",
                        status.HTTP_409_CONFLICT,
                    ) from exc
                try:
                    recovered = recover_chat_confirm_operation(
                        session_factory=idempotency_session_factory,
                        current_user=current_user,
                        thread_id=thread_id,
                        operation_id=str(payload.operation_id),
                        task_revision=payload.task_revision,
                        turn_lock=lock,
                    )
                except ChatTurnLockLostError as recovery_error:
                    raise AppException(
                        "CHAT_TURN_LOCK_LOST",
                        "会话锁已失效，无法安全核查操作结果。",
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                    ) from recovery_error
                except IdempotencyRecordError as recovery_error:
                    raise AppException(
                        "OPERATION_RESULT_UNKNOWN",
                        "该操作结果暂时无法确认，请查询状态，勿重复提交。",
                        status.HTTP_409_CONFLICT,
                    ) from recovery_error
                response = _chat_response(payload, recovered, replayed=True)
                if response.operation_status in {"PROCESSING", "UNKNOWN"}:
                    # Keep the durable receipt non-terminal so a later retry can
                    # re-check MySQL instead of replaying a stale uncertain result.
                    return response
                try:
                    complete_message_request(
                        current_user.id,
                        thread_id,
                        payload.message_request_id,
                        fingerprint=request_fingerprint,
                        response=response.model_dump(mode="json"),
                    )
                except (
                    MessageRequestConflictError,
                    MessageRequestResultUnknownError,
                ) as recovery_error:
                    raise AppException(
                        "CHAT_MESSAGE_RESULT_UNKNOWN",
                        "业务状态已核查，但消息响应回执仍无法可靠保存。",
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                    ) from recovery_error
                return response
            if receipt.disposition == "replay" and receipt.response is not None:
                replay_payload = dict(receipt.response)
                replay_payload["replayed"] = True
                return ChatResponse.model_validate(replay_payload)

        try:
            result = run_chat_agent(
                db=db,
                current_user=current_user,
                thread_id=thread_id,
                user_message=payload.message,
                ip_address=request.client.host if request.client is not None else None,
                message_request_id=(
                    str(payload.message_request_id)
                    if payload.message_request_id is not None
                    else None
                ),
                operation_id=(
                    str(payload.operation_id)
                    if payload.operation_id is not None
                    else None
                ),
                task_action=payload.task_action,
                task_revision=payload.task_revision,
                turn_lock=lock,
                idempotency_session_factory=idempotency_session_factory,
            )
        except ChatTurnLockLostError as exc:
            raise AppException(
                "CHAT_TURN_LOCK_LOST",
                "会话锁已失效，无法安全保存本轮状态；不会自动重复执行。",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
        try:
            lock.ensure_owned()
        except ChatTurnLockLostError as exc:
            raise AppException(
                "CHAT_TURN_LOCK_LOST",
                "会话锁已失效，无法安全确认本轮响应；不会自动重复执行。",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
        touch_session(current_user.id, thread_id, payload.message)
        response = _chat_response(
            payload,
            result,
            replayed=False if payload.message_request_id is not None else None,
        )
        if (
            payload.message_request_id is not None
            and request_fingerprint is not None
            and response.operation_status not in {"PROCESSING", "UNKNOWN"}
        ):
            try:
                complete_message_request(
                    current_user.id,
                    thread_id,
                    payload.message_request_id,
                    fingerprint=request_fingerprint,
                    response=response.model_dump(mode="json"),
                )
            except MessageRequestConflictError as exc:
                raise AppException(
                    "CHAT_MESSAGE_REQUEST_CONFLICT",
                    "message_request_id 已绑定到不同的消息内容。",
                    status.HTTP_409_CONFLICT,
                ) from exc
            except MessageRequestResultUnknownError as exc:
                raise AppException(
                    "CHAT_MESSAGE_RESULT_UNKNOWN",
                    "消息状态已保存，但响应回执未能可靠持久化。",
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                ) from exc
        return response
