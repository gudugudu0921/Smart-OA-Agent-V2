"""Shared Redis connection and a Redis 7 compatible LangGraph checkpointer."""

from __future__ import annotations

import base64
import time
from collections.abc import Iterator, Sequence
from functools import lru_cache
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from redis import ConnectionPool, Redis
from redis.exceptions import RedisError

from app.core.config import settings


class RedisUnavailableError(RuntimeError):
    """Raised when Redis-backed chat state cannot be accessed."""


def _redis_error(exc: RedisError) -> RedisUnavailableError:
    return RedisUnavailableError("Redis 会话服务当前不可用。")


@lru_cache
def get_redis_pool() -> ConnectionPool:
    """Return the process-wide Redis connection pool."""
    return ConnectionPool(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password.get_secret_value() or None,
        decode_responses=False,
        socket_connect_timeout=3,
        socket_timeout=5,
        health_check_interval=30,
    )


@lru_cache
def get_redis_client() -> Redis:
    """Return one Redis client backed by the shared pool."""
    return Redis(connection_pool=get_redis_pool())


def ping_redis() -> bool:
    try:
        return bool(get_redis_client().ping())
    except RedisError as exc:
        raise _redis_error(exc) from exc


def _encode_part(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class Redis7CheckpointSaver(BaseCheckpointSaver[int]):
    """Minimal synchronous LangGraph saver for plain Redis 7.

    The official ``langgraph-checkpoint-redis`` saver uses Redis Search and JSON.
    This implementation follows the installed ``BaseCheckpointSaver`` contract
    while storing opaque serializer payloads in ordinary Redis hashes and sets.
    """

    def __init__(self, client: Redis, key_prefix: str = "smart_oa") -> None:
        super().__init__()
        self.client = client
        self.key_prefix = key_prefix

    def setup(self) -> None:
        try:
            self.client.ping()
        except RedisError as exc:
            raise _redis_error(exc) from exc

    def _thread_keys_key(self, thread_id: str) -> str:
        return f"{self.key_prefix}:checkpoint_keys:{thread_id}"

    def _index_key(self, thread_id: str, checkpoint_ns: str) -> str:
        return (
            f"{self.key_prefix}:checkpoint_index:{thread_id}:"
            f"{_encode_part(checkpoint_ns)}"
        )

    def _latest_key(self, thread_id: str, checkpoint_ns: str) -> str:
        return (
            f"{self.key_prefix}:checkpoint_latest:{thread_id}:"
            f"{_encode_part(checkpoint_ns)}"
        )

    def _checkpoint_key(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> str:
        return (
            f"{self.key_prefix}:checkpoint:{thread_id}:"
            f"{_encode_part(checkpoint_ns)}:{checkpoint_id}"
        )

    def _writes_key(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> str:
        return (
            f"{self.key_prefix}:checkpoint_writes:{thread_id}:"
            f"{_encode_part(checkpoint_ns)}:{checkpoint_id}"
        )

    @staticmethod
    def _pack_typed(value: tuple[str, bytes]) -> bytes:
        type_name, payload = value
        return type_name.encode("utf-8") + b"\0" + payload

    @staticmethod
    def _unpack_typed(value: bytes) -> tuple[str, bytes]:
        type_name, payload = value.split(b"\0", 1)
        return type_name.decode("utf-8"), payload

    def _load_tuple(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        requested_config: RunnableConfig | None = None,
    ) -> CheckpointTuple | None:
        key = self._checkpoint_key(thread_id, checkpoint_ns, checkpoint_id)
        try:
            record = self.client.hgetall(key)
            if not record:
                return None
            checkpoint = self.serde.loads_typed(
                (record[b"checkpoint_type"].decode(), record[b"checkpoint"])
            )
            metadata = self.serde.loads_typed(
                (record[b"metadata_type"].decode(), record[b"metadata"])
            )
            raw_writes = self.client.hvals(
                self._writes_key(thread_id, checkpoint_ns, checkpoint_id)
            )
        except RedisError as exc:
            raise _redis_error(exc) from exc

        pending_writes = []
        for raw_write in raw_writes:
            task_id, channel, value, _task_path = self.serde.loads_typed(
                self._unpack_typed(raw_write)
            )
            pending_writes.append((task_id, channel, value))

        config = requested_config or {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }
        parent_id = record.get(b"parent_id", b"").decode()
        parent_config = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id
            else None
        )
        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = get_checkpoint_id(config)
        try:
            if not checkpoint_id:
                raw_id = self.client.get(self._latest_key(thread_id, checkpoint_ns))
                if raw_id is None:
                    return None
                checkpoint_id = raw_id.decode()
        except RedisError as exc:
            raise _redis_error(exc) from exc
        return self._load_tuple(
            thread_id,
            checkpoint_ns,
            checkpoint_id,
            config if get_checkpoint_id(config) else None,
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            return
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        exact_id = get_checkpoint_id(config)
        before_id = get_checkpoint_id(before) if before else None
        try:
            checkpoint_ids = self.client.zrevrange(
                self._index_key(thread_id, checkpoint_ns), 0, -1
            )
        except RedisError as exc:
            raise _redis_error(exc) from exc
        yielded = 0
        for raw_id in checkpoint_ids:
            checkpoint_id = raw_id.decode()
            if exact_id and checkpoint_id != exact_id:
                continue
            if before_id and checkpoint_id >= before_id:
                continue
            item = self._load_tuple(thread_id, checkpoint_ns, checkpoint_id)
            if item is None:
                continue
            if filter and not all(item.metadata.get(k) == v for k, v in filter.items()):
                continue
            yield item
            yielded += 1
            if limit is not None and yielded >= limit:
                break

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del new_versions
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = checkpoint["id"]
        parent_id = str(configurable.get("checkpoint_id", ""))
        checkpoint_type, checkpoint_payload = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_payload = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        checkpoint_key = self._checkpoint_key(
            thread_id, checkpoint_ns, checkpoint_id
        )
        index_key = self._index_key(thread_id, checkpoint_ns)
        latest_key = self._latest_key(thread_id, checkpoint_ns)
        thread_keys_key = self._thread_keys_key(thread_id)
        mapping = {
            "checkpoint_type": checkpoint_type,
            "checkpoint": checkpoint_payload,
            "metadata_type": metadata_type,
            "metadata": metadata_payload,
            "parent_id": parent_id,
        }
        try:
            with self.client.pipeline(transaction=True) as pipe:
                pipe.hset(checkpoint_key, mapping=mapping)
                pipe.zadd(index_key, {checkpoint_id: time.time()})
                pipe.set(latest_key, checkpoint_id)
                pipe.sadd(
                    thread_keys_key,
                    checkpoint_key,
                    index_key,
                    latest_key,
                    self._writes_key(thread_id, checkpoint_ns, checkpoint_id),
                )
                pipe.execute()
        except RedisError as exc:
            raise _redis_error(exc) from exc
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = str(configurable["checkpoint_id"])
        writes_key = self._writes_key(thread_id, checkpoint_ns, checkpoint_id)
        thread_keys_key = self._thread_keys_key(thread_id)
        try:
            with self.client.pipeline(transaction=True) as pipe:
                for index, (channel, value) in enumerate(writes):
                    write_index = WRITES_IDX_MAP.get(channel, index)
                    field = f"{task_id}:{write_index}"
                    payload = self._pack_typed(
                        self.serde.dumps_typed(
                            (task_id, channel, value, task_path)
                        )
                    )
                    if write_index >= 0:
                        pipe.hsetnx(writes_key, field, payload)
                    else:
                        pipe.hset(writes_key, field, payload)
                pipe.sadd(thread_keys_key, writes_key)
                pipe.execute()
        except RedisError as exc:
            raise _redis_error(exc) from exc

    def delete_thread(self, thread_id: str) -> None:
        thread_keys_key = self._thread_keys_key(thread_id)
        try:
            keys = list(self.client.smembers(thread_keys_key))
            if keys:
                self.client.delete(*keys)
            self.client.delete(thread_keys_key)
        except RedisError as exc:
            raise _redis_error(exc) from exc


@lru_cache
def get_redis_checkpointer() -> Redis7CheckpointSaver:
    saver = Redis7CheckpointSaver(
        get_redis_client(), key_prefix=settings.redis_key_prefix
    )
    saver.setup()
    return saver
