from __future__ import annotations

from datetime import date, timedelta
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.chat_agent import (
    _confirm_submit_leave_node,
    _execute_confirmed_submit_leave,
    _resolve_existing_confirmed_operation,
    project_chat_task_state,
    run_chat_agent,
)
from app.services.idempotency_service import OperationNotFoundError
from app.agents.chat_state import ChatState
from app.models.user import User


def employee() -> User:
    return User(
        id=8801,
        employee_no="TASK8801",
        username="chat_task_employee",
        password_hash="unused-in-isolated-tests",
        name="Chat Task Employee",
        role="employee",
        department="Testing",
        manager_id=8802,
        is_active=True,
        must_change_password=False,
    )


def submit_result(**fields):
    return {
        "intent": "submit_leave",
        "extracted_info": fields,
        "error": None,
    }


class ChatTaskStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saver = InMemorySaver()
        self.db = MagicMock(name="must-not-write-db")
        self.user = employee()
        self.thread_id = str(uuid4())
        self.operation_id = str(uuid4())
        self.start = date.today() + timedelta(days=20)
        self.checkpointer_patch = patch(
            "app.agents.chat_agent.get_redis_checkpointer",
            return_value=self.saver,
        )
        self.checkpointer_patch.start()

    def tearDown(self) -> None:
        self.checkpointer_patch.stop()

    def turn(
        self,
        message: str,
        recognizer,
        *,
        operation_id: str | None = None,
        revision: int | None = None,
        action: str = "message",
    ):
        return run_chat_agent(
            self.db,
            self.user,
            self.thread_id,
            message,
            recognizer=recognizer,
            message_request_id=str(uuid4()),
            operation_id=operation_id,
            task_revision=revision,
            task_action=action,
        )

    def collect_then_confirmable(self):
        first = self.turn(
            "我要请年假",
            lambda _state: submit_result(leave_type="annual"),
            operation_id=self.operation_id,
            revision=0,
        )
        second = self.turn(
            "下个月一天",
            lambda state: submit_result(
                **{
                    **state.get("extracted_info", {}),
                    "start_date": self.start,
                    "end_date": self.start,
                }
            ),
            operation_id=self.operation_id,
            revision=first["task_revision"],
        )
        return first, second

    def test_multiturn_reuses_operation_and_only_prepares_confirmation(self) -> None:
        first, second = self.collect_then_confirmable()

        self.assertEqual(first["task_phase"], "COLLECTING")
        self.assertEqual(first["active_operation_id"], self.operation_id)
        self.assertEqual(second["task_phase"], "AWAITING_CONFIRMATION")
        self.assertEqual(second["active_operation_id"], self.operation_id)
        self.assertEqual(second["task_revision"], first["task_revision"] + 1)
        self.assertEqual(second["task_fingerprint"], second["confirmation_fingerprint"])
        self.assertEqual(second["code"], "CHAT_CONFIRMATION_REQUIRED")
        self.assertFalse(second["success"])
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_unbound_candidate_is_ignored_for_general_chat(self) -> None:
        result = self.turn(
            "你好",
            lambda _state: {
                "intent": "general_chat",
                "extracted_info": {},
                "error": None,
            },
            operation_id=self.operation_id,
            revision=0,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["intent"], "general_chat")
        self.assertEqual(result["task_phase"], "IDLE")
        self.assertIsNone(result["active_operation_id"])
        self.assertIsNone(result.get("response_operation_id"))
        self.assertNotIn(self.operation_id, result["closed_operation_ids"])
        recovered = project_chat_task_state(
            result,
            user_id=self.user.id,
            thread_id=self.thread_id,
        )
        self.assertEqual(recovered["task_status"], "idle")
        self.assertIsNone(recovered["operation_id"])
        self.assertFalse(recovered["requires_confirmation"])
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_parameter_change_increments_revision_and_invalidates_old_confirmation(self) -> None:
        _, ready = self.collect_then_confirmable()
        old_revision = ready["task_revision"]
        changed = self.turn(
            "原因改为家庭事务",
            lambda state: submit_result(
                **{**state["extracted_info"], "reason": "家庭事务"}
            ),
            operation_id=self.operation_id,
            revision=old_revision,
        )
        self.assertEqual(changed["task_revision"], old_revision + 1)
        self.assertNotEqual(
            changed["confirmation_fingerprint"], ready["confirmation_fingerprint"]
        )

        stale = self.turn(
            "确认",
            lambda _state: self.fail("confirm must bypass recognition"),
            operation_id=self.operation_id,
            revision=old_revision,
            action="confirm",
        )
        self.assertEqual(stale["code"], "CHAT_TASK_REVISION_CONFLICT")
        self.db.commit.assert_not_called()

    def test_valid_confirm_without_reliable_execution_context_is_unknown(self) -> None:
        _, ready = self.collect_then_confirmable()
        confirmed = self.turn(
            "确认",
            lambda _state: self.fail("confirm must bypass recognition"),
            operation_id=self.operation_id,
            revision=ready["task_revision"],
            action="confirm",
        )
        self.assertFalse(confirmed["success"])
        self.assertEqual(confirmed["code"], "OPERATION_RESULT_UNKNOWN")
        self.assertEqual(confirmed["task_phase"], "UNKNOWN")
        self.assertIn("结果暂时无法确认", confirmed["response"])
        self.db.commit.assert_not_called()

    def test_confirmation_fingerprint_mismatch_is_rejected(self) -> None:
        _, ready = self.collect_then_confirmable()
        tampered = dict(ready)
        tampered.update(
            {
                "request_operation_id": self.operation_id,
                "request_task_revision": ready["task_revision"],
                "confirmation_fingerprint": "0" * 64,
            }
        )
        rejected = _confirm_submit_leave_node(tampered)
        self.assertEqual(rejected["code"], "CHAT_CONFIRMATION_INVALIDATED")
        self.db.commit.assert_not_called()

    def test_valid_confirmation_node_transitions_to_executing_only(self) -> None:
        _, ready = self.collect_then_confirmable()
        prepared = {
            **ready,
            "request_operation_id": self.operation_id,
            "request_task_revision": ready["task_revision"],
        }
        with patch(
            "app.tools.leave_tools.create_leave_application_idempotent"
        ) as create:
            executing = _confirm_submit_leave_node(prepared)
        self.assertEqual(executing["task_phase"], "EXECUTING")
        self.assertEqual(executing["code"], "CHAT_OPERATION_EXECUTING")
        create.assert_not_called()

    def test_execution_adapter_uses_toolnode_and_only_idempotent_service(self) -> None:
        _, ready = self.collect_then_confirmable()
        application = MagicMock()
        application.id = str(uuid4())
        application.leave_type = "annual"
        application.start_date = self.start
        application.end_date = self.start
        application.leave_days = 1
        application.status = "pending"
        application.manager_status = "pending"
        application.hr_status = "pending"
        model = MagicMock()
        model.bind_tools.return_value.invoke.return_value = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "submit_leave",
                    "args": dict(ready["task_payload_snapshot"]),
                    "id": "call-submit",
                    "type": "tool_call",
                }
            ],
        )
        with (
            patch(
                "app.agents.chat_agent._read_confirmed_operation",
                side_effect=OperationNotFoundError("not found"),
            ),
            patch(
                "app.tools.leave_tools.create_leave_application_idempotent",
                return_value=application,
            ) as create,
            patch("app.tools.leave_tools.create_leave_application") as legacy_create,
            patch("app.agents.chat_agent.get_llm", return_value=model),
        ):
            outcome = _execute_confirmed_submit_leave(
                ready,
                MagicMock(name="session_factory"),
                self.user,
                "127.0.0.1",
            )
        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["task_phase"], "SUCCEEDED")
        self.assertEqual(outcome["response_operation_status"], "SUCCEEDED")
        self.assertEqual(
            outcome["tool_result"]["data"]["application_id"], application.id
        )
        tool_messages = [
            message for message in outcome["messages"] if isinstance(message, ToolMessage)
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].tool_call_id, "call-submit")
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["operation_id"], self.operation_id)
        legacy_create.assert_not_called()

    def test_existing_non_success_statuses_are_read_only(self) -> None:
        _, ready = self.collect_then_confirmable()
        expected = {
            "PROCESSING": "EXECUTING",
            "FAILED": "FAILED",
            "UNKNOWN": "UNKNOWN",
        }
        for status, phase in expected.items():
            with self.subTest(status=status):
                with (
                    patch(
                        "app.agents.chat_agent._read_confirmed_operation",
                        return_value={
                            "status": status,
                            "result_payload": None,
                            "error_code": None,
                        },
                    ),
                    patch(
                        "app.tools.leave_tools.create_leave_application_idempotent"
                    ) as create,
                ):
                    outcome = _resolve_existing_confirmed_operation(
                        ready,
                        MagicMock(name="session_factory"),
                        self.user,
                    )
                self.assertEqual(outcome["task_phase"], phase)
                self.assertEqual(outcome["response_operation_status"], status)
                create.assert_not_called()

    def test_wrong_operation_id_cannot_confirm(self) -> None:
        _, ready = self.collect_then_confirmable()
        rejected = self.turn(
            "确认",
            lambda _state: self.fail("confirm must bypass recognition"),
            operation_id=str(uuid4()),
            revision=ready["task_revision"],
            action="confirm",
        )
        self.assertEqual(rejected["code"], "CHAT_OPERATION_ID_CONFLICT")
        self.db.commit.assert_not_called()

    def test_authenticated_user_change_cannot_take_over_task(self) -> None:
        first = self.turn(
            "我要请年假",
            lambda _state: submit_result(leave_type="annual"),
            operation_id=self.operation_id,
            revision=0,
        )
        other_user = employee()
        other_user.id = 8899
        rejected = run_chat_agent(
            self.db,
            other_user,
            self.thread_id,
            "继续",
            recognizer=lambda _state: self.fail("ownership must fail before recognition"),
            message_request_id=str(uuid4()),
            operation_id=self.operation_id,
            task_revision=first["task_revision"],
        )
        self.assertEqual(rejected["code"], "CHAT_TASK_OWNERSHIP_CONFLICT")
        self.db.commit.assert_not_called()

    def test_authenticated_user_change_cannot_confirm_task(self) -> None:
        _, ready = self.collect_then_confirmable()
        other_user = employee()
        other_user.id = 8899
        rejected = run_chat_agent(
            self.db,
            other_user,
            self.thread_id,
            "确认",
            recognizer=lambda _state: self.fail("ownership must fail first"),
            message_request_id=str(uuid4()),
            operation_id=self.operation_id,
            task_revision=ready["task_revision"],
            task_action="confirm",
        )
        self.assertEqual(rejected["code"], "CHAT_TASK_OWNERSHIP_CONFLICT")
        self.db.commit.assert_not_called()

    def test_cancel_clears_task_and_new_task_requires_new_operation_id(self) -> None:
        first, _ready = self.collect_then_confirmable()
        cancelled = self.turn(
            "取消",
            lambda _state: self.fail("cancel must bypass recognition"),
            operation_id=self.operation_id,
            revision=first["task_revision"] + 1,
            action="cancel",
        )
        self.assertTrue(cancelled["success"])
        self.assertEqual(cancelled["response_task_status"], "cancelled")
        self.assertIsNone(cancelled["active_operation_id"])
        self.assertEqual(cancelled["task_phase"], "IDLE")

        retired = self.turn(
            "再次请年假",
            lambda _state: submit_result(leave_type="annual"),
            operation_id=self.operation_id,
            revision=0,
        )
        self.assertEqual(retired["code"], "CHAT_OPERATION_ID_RETIRED")

        new_operation_id = str(uuid4())
        new_task = self.turn(
            "重新请年假",
            lambda _state: submit_result(leave_type="annual"),
            operation_id=new_operation_id,
            revision=0,
        )
        self.assertEqual(new_task["active_operation_id"], new_operation_id)
        self.assertNotEqual(new_operation_id, self.operation_id)

    def test_intent_switch_clears_submit_task_without_carrying_parameters(self) -> None:
        first = self.turn(
            "我要请年假",
            lambda _state: submit_result(leave_type="annual"),
            operation_id=self.operation_id,
            revision=0,
        )
        switched = self.turn(
            "你好",
            lambda _state: {
                "intent": "general_chat",
                "extracted_info": {},
                "error": None,
            },
            operation_id=self.operation_id,
            revision=first["task_revision"],
        )
        self.assertTrue(switched["success"])
        self.assertEqual(switched["task_phase"], "IDLE")
        self.assertIsNone(switched["active_operation_id"])
        self.assertEqual(switched["extracted_info"], {})
        self.assertEqual(switched["code"], "CHAT_TASK_SWITCHED")
        self.assertEqual(switched["response_operation_id"], self.operation_id)

    def test_legacy_client_cannot_submit_without_protocol_ids(self) -> None:
        result = run_chat_agent(
            self.db,
            self.user,
            self.thread_id,
            "明天请一天年假",
            recognizer=lambda _state: submit_result(
                leave_type="annual",
                start_date=self.start,
                end_date=self.start,
            ),
        )
        self.assertEqual(result["code"], "CHAT_OPERATION_ID_REQUIRED")
        self.assertEqual(result["task_phase"], "IDLE")
        self.db.commit.assert_not_called()

    def test_old_checkpoint_without_task_fields_is_safely_adapted(self) -> None:
        old_graph = StateGraph(ChatState)

        def seed_old_state(_state: ChatState):
            return {
                "intent": "submit_leave",
                "pending_intent": "submit_leave",
                "pending_fields": ["start_date", "end_date"],
                "current_task": "submit_leave",
                "extracted_info": {"leave_type": "annual"},
            }

        old_graph.add_node("seed", seed_old_state)
        old_graph.add_edge(START, "seed")
        old_graph.add_edge("seed", END)
        old_graph.compile(checkpointer=self.saver).invoke(
            {"messages": [HumanMessage(content="我要请年假")]},
            config={"configurable": {"thread_id": self.thread_id}},
        )

        restored = self.turn(
            "补充日期",
            lambda state: submit_result(
                **{
                    **state["extracted_info"],
                    "start_date": self.start,
                    "end_date": self.start,
                }
            ),
            operation_id=self.operation_id,
            revision=0,
        )
        self.assertEqual(restored["task_phase"], "AWAITING_CONFIRMATION")
        self.assertEqual(restored["active_operation_id"], self.operation_id)
        self.assertFalse(restored["success"])
        self.db.commit.assert_not_called()

    def test_complete_legacy_pending_checkpoint_cannot_execute_without_ids(self) -> None:
        old_graph = StateGraph(ChatState)

        def seed_complete_old_state(_state: ChatState):
            return {
                "intent": "submit_leave",
                "pending_intent": "submit_leave",
                "pending_fields": [],
                "current_task": "submit_leave",
                "extracted_info": {
                    "leave_type": "annual",
                    "start_date": self.start,
                    "end_date": self.start,
                },
            }

        old_graph.add_node("seed", seed_complete_old_state)
        old_graph.add_edge(START, "seed")
        old_graph.add_edge("seed", END)
        old_graph.compile(checkpointer=self.saver).invoke(
            {"messages": [HumanMessage(content="旧任务参数已齐全")]},
            config={"configurable": {"thread_id": self.thread_id}},
        )
        blocked = run_chat_agent(
            self.db,
            self.user,
            self.thread_id,
            "继续",
            recognizer=lambda state: submit_result(**state["extracted_info"]),
        )
        self.assertEqual(blocked["code"], "CHAT_OPERATION_ID_REQUIRED")
        self.assertEqual(blocked["task_phase"], "IDLE")
        self.db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
