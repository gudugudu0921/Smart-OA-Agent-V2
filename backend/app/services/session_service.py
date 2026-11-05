"""Redis-backed chat session metadata and ownership operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any, Literal
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, HumanMessage
from redis.exceptions import RedisError

from app.core.config import settings
from app.db.redis import (
    RedisUnavailableError,
    get_redis_checkpointer,
    get_redis_client,
)


class SessionNotFoundError(LookupError):
    """Raised for missing sessions and ownership mismatches alike."""


class ChatTurnInProgressError(RuntimeError):
    """Raised when another request owns the same user/thread turn lock."""


class ChatTurnLockLostError(RuntimeError):
    """Raised when a request no longer owns its turn lock."""


class MessageRequestConflictError(RuntimeError):
    """Raised when one request id is reused with different input."""


class MessageRequestResultUnknownError(RuntimeError):
    """Raised when a prior request started but has no durable response receipt."""


CHAT_TURN_LOCK_TTL_SECONDS = 90
CHAT_TURN_LOCK_RENEW_SECONDS = 30
_RENEW_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@dataclass(frozen=True)
class MessageRequestReceipt:
    disposition: Literal["new", "replay"]
    response: dict[str, Any] | None = None


class ChatTurnLock:
    """Renewable owner-token Redis lock for serializing one chat thread."""

    def __init__(self, client: Any, key: str, owner: str) -> None:
        self.client = client
        self.key = key
        self.owner = owner
        self._stop = Event()
        self._lost = Event()
        self._renew_thread = Thread(
            target=self._renew_loop,
            name="smart-oa-chat-lock-renewal",
            daemon=True,
        )
        self._renew_thread.start()

    def _renew_loop(self) -> None:
        while not self._stop.wait(CHAT_TURN_LOCK_RENEW_SECONDS):
            try:
                renewed = self.client.eval(
                    _RENEW_LOCK_SCRIPT,
                    1,
                    self.key,
                    self.owner,
                    CHAT_TURN_LOCK_TTL_SECONDS,
                )
            except RedisError:
                self._lost.set()
                return
            if renewed != 1:
                self._lost.set()
                return

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise ChatTurnLockLostError("Chat turn lock ownership was lost")
        try:
            owned = self.client.get(self.key)
        except RedisError as exc:
            raise RedisUnavailableError("Redis 会话服务当前不可用。") from exc
        if owned is None or owned.decode() != self.owner:
            self._lost.set()
            raise ChatTurnLockLostError("Chat turn lock ownership was lost")

    def release(self) -> None:
        self._stop.set()
        self._renew_thread.join(timeout=1)
        try:
            self.client.eval(
                _RELEASE_LOCK_SCRIPT,
                1,
                self.key,
                self.owner,
            )
        except RedisError as exc:
            raise RedisUnavailableError("Redis 会话服务当前不可用。") from exc

    def __enter__(self) -> "ChatTurnLock":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _metadata_key(user_id: int, thread_id: str) -> str:
    return f"{settings.redis_key_prefix}:session:{user_id}:{thread_id}"


def _index_key(user_id: int) -> str:
    return f"{settings.redis_key_prefix}:user_sessions:{user_id}"


def _turn_lock_key(user_id: int, thread_id: str) -> str:
    return f"{settings.redis_key_prefix}:chat_turn_lock:{user_id}:{thread_id}"


def _message_receipt_key(user_id: int, thread_id: str, request_id: str) -> str:
    return (
        f"{settings.redis_key_prefix}:chat_message_receipt:"
        f"{user_id}:{thread_id}:{request_id}"
    )


def _decode_metadata(raw: dict[bytes, bytes]) -> dict[str, str]:
    return {key.decode(): value.decode() for key, value in raw.items()}


def _execute(operation):
    try:
        return operation()
    except RedisError as exc:
        raise RedisUnavailableError("Redis 会话服务当前不可用。") from exc


def acquire_chat_turn_lock(user_id: int, thread_id: UUID | str) -> ChatTurnLock:
    """Acquire one renewable lock without granting any business-write guarantee."""
    normalized = str(thread_id)
    owner = str(uuid4())
    client = get_redis_client()
    acquired = _execute(
        lambda: client.set(
            _turn_lock_key(user_id, normalized),
            owner,
            nx=True,
            ex=CHAT_TURN_LOCK_TTL_SECONDS,
        )
    )
    if not acquired:
        raise ChatTurnInProgressError("Another chat turn is already in progress")
    return ChatTurnLock(client, _turn_lock_key(user_id, normalized), owner)


def calculate_message_request_fingerprint(
    *,
    message: str,
    operation_id: UUID | str | None,
    task_action: str,
    task_revision: int | None,
) -> str:
    canonical = json.dumps(
        {
            "message": message,
            "operation_id": None if operation_id is None else str(operation_id),
            "task_action": task_action,
            "task_revision": task_revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def begin_message_request(
    user_id: int,
    thread_id: UUID | str,
    request_id: UUID | str,
    *,
    fingerprint: str,
) -> MessageRequestReceipt:
    """Reserve a durable request receipt or replay its completed safe response.

    A PROCESSING receipt is deliberately not re-executed: checkpoint and receipt
    writes are not atomic, so its outcome must be treated as unknown.
    """
    key = _message_receipt_key(user_id, str(thread_id), str(request_id))
    initial = json.dumps(
        {"fingerprint": fingerprint, "status": "PROCESSING"},
        separators=(",", ":"),
    )

    def operation():
        client = get_redis_client()
        options = {"nx": True}
        if settings.session_ttl_seconds > 0:
            options["ex"] = settings.session_ttl_seconds
        created = client.set(key, initial, **options)
        if created:
            return None
        return client.get(key)

    existing = _execute(operation)
    if existing is None:
        return MessageRequestReceipt("new")
    try:
        receipt = json.loads(existing.decode())
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        raise MessageRequestResultUnknownError(
            "Stored message receipt cannot be decoded"
        ) from exc
    if receipt.get("fingerprint") != fingerprint:
        raise MessageRequestConflictError(
            "message_request_id is already bound to different input"
        )
    if receipt.get("status") == "COMPLETED" and isinstance(
        receipt.get("response"), dict
    ):
        return MessageRequestReceipt("replay", receipt["response"])
    raise MessageRequestResultUnknownError(
        "A prior message request has no durable completed response"
    )


def complete_message_request(
    user_id: int,
    thread_id: UUID | str,
    request_id: UUID | str,
    *,
    fingerprint: str,
    response: dict[str, Any],
) -> None:
    key = _message_receipt_key(user_id, str(thread_id), str(request_id))
    completed = json.dumps(
        {
            "fingerprint": fingerprint,
            "status": "COMPLETED",
            "response": response,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    def operation():
        client = get_redis_client()
        raw = client.get(key)
        if raw is None:
            raise MessageRequestResultUnknownError("Message receipt disappeared")
        try:
            current = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise MessageRequestResultUnknownError(
                "Stored message receipt cannot be decoded"
            ) from exc
        if current.get("fingerprint") != fingerprint:
            raise MessageRequestConflictError(
                "message_request_id is already bound to different input"
            )
        options: dict[str, Any] = {}
        if settings.session_ttl_seconds > 0:
            options["ex"] = settings.session_ttl_seconds
        client.set(key, completed, **options)

    _execute(operation)


def create_session(user_id: int) -> dict[str, str]:
    thread_id = str(uuid4())
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    metadata = {
        "thread_id": thread_id,
        "user_id": str(user_id),
        "title": "新会话",
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_message_preview": "",
    }

    def operation():
        client = get_redis_client()
        with client.pipeline(transaction=True) as pipe:
            pipe.hset(_metadata_key(user_id, thread_id), mapping=metadata)
            if settings.session_ttl_seconds > 0:
                pipe.expire(
                    _metadata_key(user_id, thread_id), settings.session_ttl_seconds
                )
            pipe.zadd(_index_key(user_id), {thread_id: now.timestamp()})
            if settings.session_ttl_seconds > 0:
                pipe.expire(_index_key(user_id), settings.session_ttl_seconds)
            pipe.execute()

    _execute(operation)
    return metadata


def get_session(user_id: int, thread_id: UUID | str) -> dict[str, str]:
    normalized = str(thread_id)
    raw = _execute(
        lambda: get_redis_client().hgetall(_metadata_key(user_id, normalized))
    )
    if not raw:
        raise SessionNotFoundError("会话不存在。")
    metadata = _decode_metadata(raw)
    if metadata.get("user_id") != str(user_id):
        raise SessionNotFoundError("会话不存在。")
    return metadata


def validate_session_owner(user_id: int, thread_id: UUID | str) -> dict[str, str]:
    return get_session(user_id, thread_id)


def get_session_messages(
    user_id: int, thread_id: UUID | str
) -> list[dict[str, str]]:
    """Read the public human/AI message history from the latest checkpoint."""
    validate_session_owner(user_id, thread_id)
    normalized = str(thread_id)
    checkpoint = get_redis_checkpointer().get_tuple(
        {"configurable": {"thread_id": normalized}}
    )
    if checkpoint is None:
        return []

    result: list[dict[str, str]] = []
    state = checkpoint.checkpoint.get("channel_values", {})
    for message in state.get("messages", []):
        if not isinstance(message.content, str):
            continue
        if isinstance(message, HumanMessage):
            result.append({"role": "user", "content": message.content})
        elif isinstance(message, AIMessage):
            if (
                message.tool_calls
                or message.additional_kwargs.get("smart_oa_internal_tool_call")
                or not message.content.strip()
            ):
                continue
            result.append({"role": "assistant", "content": message.content})
    return result


def get_session_task_checkpoint(
    user_id: int, thread_id: UUID | str
) -> dict[str, Any]:
    """Read the latest owned checkpoint state without executing or mutating it."""
    validate_session_owner(user_id, thread_id)
    normalized = str(thread_id)
    checkpoint = get_redis_checkpointer().get_tuple(
        {"configurable": {"thread_id": normalized}}
    )
    if checkpoint is None:
        return {}
    state = checkpoint.checkpoint.get("channel_values", {})
    return dict(state) if isinstance(state, dict) else {}


def list_sessions(user_id: int) -> list[dict[str, str]]:
    def operation():
        client = get_redis_client()
        thread_ids = client.zrevrange(_index_key(user_id), 0, -1)
        result = []
        stale = []
        for raw_thread_id in thread_ids:
            thread_id = raw_thread_id.decode()
            raw = client.hgetall(_metadata_key(user_id, thread_id))
            if raw:
                result.append(_decode_metadata(raw))
            else:
                stale.append(thread_id)
        if stale:
            client.zrem(_index_key(user_id), *stale)
        return result

    return _execute(operation)


def _title_for_message(message: str) -> str:
    if any(word in message for word in ("查询", "查看", "记录", "待审批")):
        return "请假查询"
    if any(word in message for word in ("请假", "年假", "事假", "病假")):
        return "请假申请"
    if any(word in message for word in ("批准", "拒绝", "审批")):
        return "请假审批"
    return "OA 助手会话"


def touch_session(user_id: int, thread_id: UUID | str, message: str) -> dict[str, str]:
    metadata = get_session(user_id, thread_id)
    normalized = str(thread_id)
    now = datetime.now(UTC)
    updates = {
        "updated_at": now.isoformat(),
        "last_message_preview": message[:100],
    }
    if metadata.get("title") == "新会话":
        updates["title"] = _title_for_message(message)

    def operation():
        client = get_redis_client()
        with client.pipeline(transaction=True) as pipe:
            pipe.hset(_metadata_key(user_id, normalized), mapping=updates)
            if settings.session_ttl_seconds > 0:
                pipe.expire(
                    _metadata_key(user_id, normalized),
                    settings.session_ttl_seconds,
                )
            pipe.zadd(_index_key(user_id), {normalized: now.timestamp()})
            if settings.session_ttl_seconds > 0:
                pipe.expire(_index_key(user_id), settings.session_ttl_seconds)
            pipe.execute()

    _execute(operation)
    return {**metadata, **updates}


def delete_session(user_id: int, thread_id: UUID | str) -> None:
    validate_session_owner(user_id, thread_id)
    normalized = str(thread_id)
    get_redis_checkpointer().delete_thread(normalized)

    def operation():
        client = get_redis_client()
        with client.pipeline(transaction=True) as pipe:
            pipe.delete(_metadata_key(user_id, normalized))
            pipe.zrem(_index_key(user_id), normalized)
            pipe.execute()

    _execute(operation)
