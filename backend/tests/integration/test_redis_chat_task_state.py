from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from redis import Redis

from app.agents.chat_state import ChatState
from app.agents.chat_agent import run_chat_agent
from app.db.redis import Redis7CheckpointSaver
from app.models.user import User
from app.services.idempotency_service import calculate_request_fingerprint
from app.services.session_service import (
    ChatTurnInProgressError,
    ChatTurnLockLostError,
    acquire_chat_turn_lock,
    begin_message_request,
    calculate_message_request_fingerprint,
    complete_message_request,
    get_session_task_checkpoint,
    SessionNotFoundError,
)


REDIS_URL = os.getenv("SMART_OA_CHAT_TEST_REDIS_URL")


@unittest.skipUnless(
    REDIS_URL,
    "SMART_OA_CHAT_TEST_REDIS_URL is required for real Redis integration tests",
)
class RedisChatTaskStateIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Redis.from_url(REDIS_URL, decode_responses=False)
        self.client.ping()
        self.prefix = f"smartoa-phase2a4b-{uuid4()}"
        self.thread_id = str(uuid4())

    def tearDown(self) -> None:
        # Delete only keys created under this test's random prefix.
        keys = list(self.client.scan_iter(match=f"{self.prefix}:*", count=100))
        if keys:
            self.client.delete(*keys)
        self.client.close()

    def test_checkpoint_task_state_restores_in_a_separate_process(self) -> None:
        saver = Redis7CheckpointSaver(self.client, key_prefix=self.prefix)
        saver.setup()
        operation_id = str(uuid4())
        fingerprint = "a" * 64
        graph = StateGraph(ChatState)

        def prepare(_state: ChatState):
            return {
                "intent": "submit_leave",
                "pending_intent": "submit_leave",
                "current_task": "submit_leave",
                "active_operation_id": operation_id,
                "active_operation_type": "SUBMIT_LEAVE",
                "task_phase": "AWAITING_CONFIRMATION",
                "task_revision": 2,
                "task_payload_snapshot": {
                    "leave_type": "annual",
                    "start_date": "2030-01-01",
                    "end_date": "2030-01-01",
                    "reason": None,
                },
                "task_fingerprint": fingerprint,
                "confirmation_fingerprint": fingerprint,
                "messages": [AIMessage(content="请确认")],
            }

        graph.add_node("prepare", prepare)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", END)
        graph.compile(checkpointer=saver).invoke(
            {"messages": [HumanMessage(content="我要请年假")]},
            config={"configurable": {"thread_id": self.thread_id}},
        )

        script = """
import json, os
from redis import Redis
from app.db.redis import Redis7CheckpointSaver
client = Redis.from_url(os.environ['SMART_OA_CHAT_TEST_REDIS_URL'], decode_responses=False)
saver = Redis7CheckpointSaver(client, key_prefix=os.environ['PHASE2A4B_PREFIX'])
item = saver.get_tuple({'configurable': {'thread_id': os.environ['PHASE2A4B_THREAD']}})
state = item.checkpoint['channel_values']
print(json.dumps({
    'types': [message.type for message in state['messages']],
    'operation_id': state['active_operation_id'],
    'phase': state['task_phase'],
    'revision': state['task_revision'],
    'snapshot': state['task_payload_snapshot'],
    'fingerprints_match': state['task_fingerprint'] == state['confirmation_fingerprint'],
}, sort_keys=True))
client.close()
"""
        environment = os.environ.copy()
        environment["PHASE2A4B_PREFIX"] = self.prefix
        environment["PHASE2A4B_THREAD"] = self.thread_id
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        restored = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(restored["types"], ["human", "ai"])
        self.assertEqual(restored["operation_id"], operation_id)
        self.assertEqual(restored["phase"], "AWAITING_CONFIRMATION")
        self.assertEqual(restored["revision"], 2)
        self.assertEqual(restored["snapshot"]["leave_type"], "annual")
        self.assertTrue(restored["fingerprints_match"])

    def test_safe_task_projection_restores_in_a_separate_process(self) -> None:
        saver = Redis7CheckpointSaver(self.client, key_prefix=self.prefix)
        saver.setup()
        user_id = 9010
        operation_id = str(uuid4())
        snapshot = {
            "leave_type": "annual",
            "start_date": "2030-02-01",
            "end_date": "2030-02-02",
            "reason": "跨进程恢复测试",
        }
        fingerprint = calculate_request_fingerprint("SUBMIT_LEAVE", snapshot)
        graph = StateGraph(ChatState)

        def prepare(_state: ChatState):
            return {
                "active_operation_id": operation_id,
                "active_operation_type": "SUBMIT_LEAVE",
                "active_task_user_id": user_id,
                "active_task_thread_id": self.thread_id,
                "task_phase": "AWAITING_CONFIRMATION",
                "task_revision": 4,
                "task_payload_snapshot": snapshot,
                "task_fingerprint": fingerprint,
                "confirmation_fingerprint": fingerprint,
            }

        graph.add_node("prepare", prepare)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", END)
        graph.compile(checkpointer=saver).invoke(
            {"messages": [HumanMessage(content="准备请假确认")]},
            config={"configurable": {"thread_id": self.thread_id}},
        )
        self.client.hset(
            f"{self.prefix}:session:{user_id}:{self.thread_id}",
            mapping={
                "thread_id": self.thread_id,
                "user_id": str(user_id),
                "title": "隔离恢复测试",
                "created_at": "2030-01-01T00:00:00+00:00",
                "updated_at": "2030-01-01T00:00:00+00:00",
                "last_message_preview": "准备请假确认",
            },
        )

        script = """
import json, os
from redis import Redis
from unittest.mock import patch
from app.agents.chat_agent import project_chat_task_state
from app.db.redis import Redis7CheckpointSaver
from app.services.session_service import _metadata_key, get_session_task_checkpoint
client = Redis.from_url(os.environ['SMART_OA_CHAT_TEST_REDIS_URL'], decode_responses=False)
saver = Redis7CheckpointSaver(client, key_prefix=os.environ['PHASE2A4D0_PREFIX'])
metadata_key = f"{os.environ['PHASE2A4D0_PREFIX']}:session:{os.environ['PHASE2A4D0_USER']}:{os.environ['PHASE2A4D0_THREAD']}"
assert client.exists(metadata_key), f"missing metadata key: {metadata_key}"
with patch('app.services.session_service.settings.redis_key_prefix', os.environ['PHASE2A4D0_PREFIX']):
    assert _metadata_key(int(os.environ['PHASE2A4D0_USER']), os.environ['PHASE2A4D0_THREAD']) == metadata_key
    with patch('app.services.session_service.get_redis_client', return_value=client):
        with patch('app.services.session_service.get_redis_checkpointer', return_value=saver):
            state = get_session_task_checkpoint(int(os.environ['PHASE2A4D0_USER']), os.environ['PHASE2A4D0_THREAD'])
view = project_chat_task_state(
    state,
    user_id=int(os.environ['PHASE2A4D0_USER']),
    thread_id=os.environ['PHASE2A4D0_THREAD'],
)
print(json.dumps(view, sort_keys=True))
client.close()
"""
        environment = os.environ.copy()
        environment["PHASE2A4D0_PREFIX"] = self.prefix
        environment["PHASE2A4D0_THREAD"] = self.thread_id
        environment["PHASE2A4D0_USER"] = str(user_id)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        restored = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(restored["operation_id"], operation_id)
        self.assertEqual(restored["task_status"], "awaiting_confirmation")
        self.assertEqual(restored["task_revision"], 4)
        self.assertTrue(restored["requires_confirmation"])
        self.assertEqual(restored["confirmation"]["reason"], "跨进程恢复测试")
        self.assertIsNone(restored["operation_status"])

    def test_task_checkpoint_read_is_owned_and_does_not_mutate_redis(self) -> None:
        saver = Redis7CheckpointSaver(self.client, key_prefix=self.prefix)
        saver.setup()
        user_id = 9011
        graph = StateGraph(ChatState)
        graph.add_node(
            "collect",
            lambda _state: {
                "active_operation_id": str(uuid4()),
                "active_operation_type": "SUBMIT_LEAVE",
                "active_task_user_id": user_id,
                "active_task_thread_id": self.thread_id,
                "task_phase": "COLLECTING",
                "task_revision": 1,
            },
        )
        graph.add_edge(START, "collect")
        graph.add_edge("collect", END)
        graph.compile(checkpointer=saver).invoke(
            {"messages": [HumanMessage(content="我要请假")]},
            config={"configurable": {"thread_id": self.thread_id}},
        )
        metadata_key = f"{self.prefix}:session:{user_id}:{self.thread_id}"
        self.client.hset(
            metadata_key,
            mapping={"thread_id": self.thread_id, "user_id": str(user_id)},
        )
        before = saver.get_tuple(
            {"configurable": {"thread_id": self.thread_id}}
        )

        with patch(
            "app.services.session_service.settings.redis_key_prefix", self.prefix
        ):
            with patch(
                "app.services.session_service.get_redis_client",
                return_value=self.client,
            ):
                with patch(
                    "app.services.session_service.get_redis_checkpointer",
                    return_value=saver,
                ):
                    state = get_session_task_checkpoint(user_id, self.thread_id)
                    with self.assertRaises(SessionNotFoundError):
                        get_session_task_checkpoint(user_id + 1, self.thread_id)

        after = saver.get_tuple(
            {"configurable": {"thread_id": self.thread_id}}
        )
        self.assertEqual(state["task_phase"], "COLLECTING")
        self.assertEqual(before.checkpoint, after.checkpoint)
        self.assertEqual(before.config, after.config)

    def test_real_redis_lock_and_receipt_are_cross_client_visible(self) -> None:
        second_client = Redis.from_url(REDIS_URL, decode_responses=False)
        thread_id = str(uuid4())
        request_id = str(uuid4())
        fingerprint = calculate_message_request_fingerprint(
            message="隔离 Redis 测试",
            operation_id=None,
            task_action="message",
            task_revision=None,
        )
        with patch("app.services.session_service.settings.redis_key_prefix", self.prefix):
            with patch(
                "app.services.session_service.get_redis_client",
                return_value=self.client,
            ):
                first_lock = acquire_chat_turn_lock(9001, thread_id)
                begin_message_request(
                    9001, thread_id, request_id, fingerprint=fingerprint
                )
            try:
                lock_script = """
import os
from redis import Redis
from unittest.mock import patch
from app.services.session_service import ChatTurnInProgressError, acquire_chat_turn_lock
client = Redis.from_url(os.environ['SMART_OA_CHAT_TEST_REDIS_URL'], decode_responses=False)
with patch('app.services.session_service.settings.redis_key_prefix', os.environ['PHASE2A4B_PREFIX']):
    with patch('app.services.session_service.get_redis_client', return_value=client):
        try:
            lock = acquire_chat_turn_lock(9001, os.environ['PHASE2A4B_THREAD'])
        except ChatTurnInProgressError:
            print('blocked')
        else:
            lock.release()
            print('unexpected-acquire')
client.close()
"""
                environment = os.environ.copy()
                environment["PHASE2A4B_PREFIX"] = self.prefix
                environment["PHASE2A4B_THREAD"] = thread_id
                contender = subprocess.run(
                    [sys.executable, "-c", lock_script],
                    cwd=Path(__file__).resolve().parents[2],
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(contender.stdout.strip().splitlines()[-1], "blocked")
                with patch(
                    "app.services.session_service.get_redis_client",
                    return_value=second_client,
                ):
                    with self.assertRaises(ChatTurnInProgressError):
                        acquire_chat_turn_lock(9001, thread_id)
                    complete_message_request(
                        9001,
                        thread_id,
                        request_id,
                        fingerprint=fingerprint,
                        response={"success": True, "response": "ok"},
                    )
                    replay = begin_message_request(
                        9001, thread_id, request_id, fingerprint=fingerprint
                    )
                self.assertEqual(replay.disposition, "replay")
                self.assertEqual(replay.response["response"], "ok")
            finally:
                first_lock.release()
                second_client.close()

    def test_guarded_graph_publishes_final_checkpoint_while_owner_is_current(self) -> None:
        saver = Redis7CheckpointSaver(self.client, key_prefix=self.prefix)
        current_user = User(
            id=9002,
            employee_no="REDIS9002",
            username="redis_guard_user",
            password_hash="unused-in-isolated-tests",
            name="Redis Guard User",
            role="employee",
            department="Testing",
            manager_id=9003,
            is_active=True,
            must_change_password=False,
        )
        with patch("app.services.session_service.settings.redis_key_prefix", self.prefix):
            with patch(
                "app.services.session_service.get_redis_client",
                return_value=self.client,
            ):
                turn_lock = acquire_chat_turn_lock(current_user.id, self.thread_id)
            try:
                with patch(
                    "app.agents.chat_agent.get_redis_checkpointer",
                    return_value=saver,
                ):
                    result = run_chat_agent(
                        MagicMock(),
                        current_user,
                        self.thread_id,
                        "你好",
                        recognizer=lambda _state: {
                            "intent": "general_chat",
                            "extracted_info": {},
                            "error": None,
                        },
                        turn_lock=turn_lock,
                    )
                self.assertTrue(result["success"])
                restored = saver.get_tuple(
                    {"configurable": {"thread_id": self.thread_id}}
                )
                self.assertIsNotNone(restored)
                self.assertEqual(
                    restored.checkpoint["channel_values"]["response"],
                    result["response"],
                )
            finally:
                turn_lock.release()

    def test_lost_owner_cannot_publish_stale_graph_checkpoint(self) -> None:
        saver = Redis7CheckpointSaver(self.client, key_prefix=self.prefix)
        current_user = User(
            id=9004,
            employee_no="REDIS9004",
            username="redis_lost_owner",
            password_hash="unused-in-isolated-tests",
            name="Redis Lost Owner",
            role="employee",
            department="Testing",
            manager_id=9005,
            is_active=True,
            must_change_password=False,
        )
        with patch("app.services.session_service.settings.redis_key_prefix", self.prefix):
            with patch(
                "app.services.session_service.get_redis_client",
                return_value=self.client,
            ):
                turn_lock = acquire_chat_turn_lock(current_user.id, self.thread_id)

            def steal_lock(_state):
                self.client.set(turn_lock.key, "new-owner", ex=90)
                return {
                    "intent": "general_chat",
                    "extracted_info": {},
                    "error": None,
                }

            try:
                with patch(
                    "app.agents.chat_agent.get_redis_checkpointer",
                    return_value=saver,
                ):
                    with self.assertRaises(ChatTurnLockLostError):
                        run_chat_agent(
                            MagicMock(),
                            current_user,
                            self.thread_id,
                            "你好",
                            recognizer=steal_lock,
                            turn_lock=turn_lock,
                        )
                self.assertIsNone(
                    saver.get_tuple(
                        {"configurable": {"thread_id": self.thread_id}}
                    )
                )
                self.assertEqual(self.client.get(turn_lock.key), b"new-owner")
            finally:
                turn_lock.release()

    def test_guarded_checkpoint_preserves_tool_call_and_tool_message(self) -> None:
        saver = Redis7CheckpointSaver(self.client, key_prefix=self.prefix)
        current_user = User(
            id=9006,
            employee_no="REDIS9006",
            username="redis_toolnode_user",
            password_hash="unused-in-isolated-tests",
            name="Redis ToolNode User",
            role="employee",
            department="Testing",
            manager_id=9007,
            is_active=True,
            must_change_password=False,
        )
        model = MagicMock()
        model.bind_tools.return_value.invoke.return_value = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "query_my_leave",
                    "args": {},
                    "id": "call-guarded-checkpoint",
                    "type": "tool_call",
                }
            ],
        )
        with patch("app.services.session_service.settings.redis_key_prefix", self.prefix):
            with patch(
                "app.services.session_service.get_redis_client",
                return_value=self.client,
            ):
                turn_lock = acquire_chat_turn_lock(current_user.id, self.thread_id)
            try:
                with (
                    patch(
                        "app.agents.chat_agent.get_redis_checkpointer",
                        return_value=saver,
                    ),
                    patch("app.agents.chat_agent.get_llm", return_value=model),
                    patch(
                        "app.tools.query_tools.query_my_leave_tool",
                        return_value={"success": True, "data": []},
                    ),
                ):
                    result = run_chat_agent(
                        MagicMock(),
                        current_user,
                        self.thread_id,
                        "查询我的请假记录",
                        recognizer=lambda _state: {
                            "intent": "query_my_leave",
                            "extracted_info": {},
                            "error": None,
                        },
                        turn_lock=turn_lock,
                    )
                self.assertTrue(result["success"])
                restored = saver.get_tuple(
                    {"configurable": {"thread_id": self.thread_id}}
                )
                messages = restored.checkpoint["channel_values"]["messages"]
                call_message = next(
                    message
                    for message in messages
                    if isinstance(message, AIMessage) and message.tool_calls
                )
                tool_message = next(
                    message for message in messages if isinstance(message, ToolMessage)
                )
                self.assertEqual(
                    call_message.tool_calls[0]["id"], "call-guarded-checkpoint"
                )
                self.assertEqual(
                    tool_message.tool_call_id, "call-guarded-checkpoint"
                )
            finally:
                turn_lock.release()


if __name__ == "__main__":
    unittest.main()
