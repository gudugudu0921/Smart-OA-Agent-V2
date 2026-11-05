from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.chat_agent import (
    _cancel_submit_leave_task_node,
    _confirm_submit_leave_node,
    _execute_confirmed_submit_leave,
    run_chat_agent,
)
from app.models.user import User
from app.services.idempotency_service import (
    OperationInProgressError,
    OperationNotFoundError,
    OperationPreviouslyFailedError,
    OperationResultUnknownError,
)
from app.tools.leave_tools import build_submit_leave_tool


def employee(user_id: int = 110, role: str = "employee") -> User:
    now = datetime.now()
    return User(
        id=user_id,
        username=f"submit_tool_{role}_{user_id}",
        employee_no=f"SUBMIT{user_id:03d}",
        password_hash="not-used",
        name=f"Submit Tool {role} {user_id}",
        role=role,
        department="Testing",
        manager_id=111,
        is_active=True,
        must_change_password=False,
        created_at=now,
        updated_at=now,
    )


def application(application_id: str, start: date):
    return SimpleNamespace(
        id=application_id,
        employee_id=110,
        leave_type="annual",
        start_date=start,
        end_date=start + timedelta(days=1),
        leave_days=2,
        reason="family trip",
        status="pending",
        manager_status="pending",
        manager_id=111,
        hr_status="pending",
    )


def call(
    name: str,
    payload: dict,
    call_id: str = "call-submit-leave",
    **extra,
):
    return {
        "name": name,
        "args": {**payload, **extra},
        "id": call_id,
        "type": "tool_call",
    }


class RecordingToolCallingModel:
    def __init__(self, response) -> None:
        self.response = response
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def invoke(self, _messages):
        return self.response


class FlushableInMemorySaver(InMemorySaver):
    def flush(self) -> None:
        return None


class SubmitLeaveToolNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.employee = employee()
        self.start = date.today() + timedelta(days=30)
        self.payload = {
            "leave_type": "annual",
            "start_date": self.start.isoformat(),
            "end_date": (self.start + timedelta(days=1)).isoformat(),
            "reason": "family trip",
        }
        self.application_id = str(uuid4())
        self.operation_id = str(uuid4())
        self.thread_id = str(uuid4())
        self.message_request_id = str(uuid4())

    def recognizer(self, _state):
        return {
            "intent": "submit_leave",
            "extracted_info": dict(self.payload),
            "reference_index": None,
            "cancel_current_task": False,
            "error": None,
        }

    def prepare(self, *, saver=None):
        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=saver or InMemorySaver(),
            ),
            patch("app.tools.leave_tools.create_leave_application_idempotent") as service,
        ):
            result = run_chat_agent(
                MagicMock(name="request-db"),
                self.employee,
                self.thread_id,
                "我要请年假",
                recognizer=self.recognizer,
                message_request_id=self.message_request_id,
                operation_id=self.operation_id,
                task_revision=0,
            )
        service.assert_not_called()
        return result

    def confirmed_state(self):
        prepared = self.prepare()
        confirmation = _confirm_submit_leave_node(
            {
                **prepared,
                "request_task_action": "confirm",
                "request_operation_id": self.operation_id,
                "request_task_revision": prepared["task_revision"],
            }
        )
        return {**prepared, **confirmation}

    def execute_with(
        self,
        model_response,
        *,
        service_result=None,
        service_error=None,
        read_side_effect=None,
    ):
        state = self.confirmed_state()
        model = RecordingToolCallingModel(model_response)
        service_patch = (
            {"side_effect": service_error}
            if service_error is not None
            else {
                "return_value": service_result
                or application(self.application_id, self.start)
            }
        )
        read_effect = read_side_effect or OperationNotFoundError("not found")
        with (
            patch("app.agents.chat_agent.get_llm", return_value=model),
            patch(
                "app.agents.chat_agent._read_confirmed_operation",
                side_effect=read_effect,
            ) as read,
            patch(
                "app.tools.leave_tools.create_leave_application_idempotent",
                **service_patch,
            ) as service,
        ):
            result = _execute_confirmed_submit_leave(
                state,
                MagicMock(name="session-factory"),
                self.employee,
                "127.0.0.1",
            )
        return result, model, service, read

    def test_preparation_requires_structured_confirmation_without_write(self) -> None:
        result = self.prepare()
        self.assertEqual(result["task_phase"], "AWAITING_CONFIRMATION")
        self.assertEqual(result["active_operation_type"], "SUBMIT_LEAVE")
        self.assertEqual(result["response_confirmation"]["leave_type"], "annual")
        self.assertTrue(result["requires_confirmation"])

    def test_confirmed_submit_runs_once_through_toolnode_with_call_id(self) -> None:
        call_id = "call-submit-once"
        result, model, service, _read = self.execute_with(
            AIMessage(content="", tool_calls=[call("submit_leave", self.payload, call_id)])
        )
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].tool_call_id, call_id)
        self.assertEqual(tool_messages[0].name, "submit_leave")
        self.assertEqual(model.bound_tools[0].name, "submit_leave")
        self.assertTrue(result["success"])
        self.assertEqual(result["tool_result"]["data"]["application_id"], self.application_id)
        service.assert_called_once()

    def test_tool_schema_contains_only_leave_business_fields(self) -> None:
        tool = build_submit_leave_tool(
            MagicMock(),
            self.employee,
            operation_id=self.operation_id,
            confirmed_payload=self.payload,
        )
        schema = tool.args_schema.model_json_schema()
        self.assertEqual(
            set(schema["properties"]),
            {"leave_type", "start_date", "end_date", "reason"},
        )
        serialized = repr(schema).lower()
        for forbidden in (
            "current_user", "user_id", "manager_id", "operation_id", "session",
            "thread_id", "fingerprint", "revision", "jwt", "confirmation",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_tool_defense_rejects_role_and_confirmed_payload_mismatch(self) -> None:
        cases = (
            (employee(112, "manager"), self.payload),
            (self.employee, {**self.payload, "leave_type": "sick"}),
            (self.employee, {**self.payload, "start_date": (self.start + timedelta(days=1)).isoformat()}),
            (self.employee, {**self.payload, "end_date": (self.start + timedelta(days=2)).isoformat()}),
            (self.employee, {**self.payload, "reason": "changed"}),
        )
        for actor, payload in cases:
            with self.subTest(role=actor.role, payload=payload):
                tool = build_submit_leave_tool(
                    MagicMock(), actor,
                    operation_id=self.operation_id,
                    confirmed_payload=self.payload,
                )
                with patch("app.tools.leave_tools.create_leave_application_idempotent") as service:
                    message = tool.invoke(call("submit_leave", payload))
                self.assertFalse(message.artifact["success"])
                service.assert_not_called()

    def test_invalid_tool_calls_fail_before_service(self) -> None:
        cases = (
            AIMessage(content="直接回答"),
            AIMessage(content="", tool_calls=[call("cancel_leave", self.payload)]),
            AIMessage(content="", tool_calls=[call("submit_leave", {**self.payload, "reason": "changed"})]),
            AIMessage(content="", tool_calls=[call("submit_leave", self.payload, user_id=110)]),
            AIMessage(content="", tool_calls=[call("submit_leave", self.payload, "one"), call("submit_leave", self.payload, "two")]),
            AIMessage(content="", tool_calls=[call("submit_leave", self.payload, "")]),
        )
        for response in cases:
            with self.subTest(response=response):
                result, _model, service, _read = self.execute_with(response)
                self.assertFalse(result["success"])
                self.assertEqual(result["task_phase"], "FAILED")
                service.assert_not_called()

    def test_cancel_and_tampered_confirmation_never_execute(self) -> None:
        prepared = self.prepare()
        cancelled = _cancel_submit_leave_task_node(
            {
                **prepared,
                "request_operation_id": self.operation_id,
                "request_task_revision": prepared["task_revision"],
            }
        )
        self.assertEqual(cancelled["task_phase"], "IDLE")
        tampered = _confirm_submit_leave_node(
            {
                **prepared,
                "task_payload_snapshot": {**prepared["task_payload_snapshot"], "reason": "changed"},
                "request_operation_id": self.operation_id,
                "request_task_revision": prepared["task_revision"],
            }
        )
        self.assertEqual(tampered["code"], "CHAT_CONFIRMATION_INVALIDATED")

    def test_existing_success_replays_without_llm_or_second_service_call(self) -> None:
        state = self.confirmed_state()
        existing = {
            "status": "SUCCEEDED",
            "result_payload": {"application_id": self.application_id},
            "error_code": None,
        }
        with (
            patch("app.agents.chat_agent._read_confirmed_operation", return_value=existing),
            patch("app.agents.chat_agent.get_llm") as llm,
            patch("app.tools.leave_tools.create_leave_application_idempotent") as service,
        ):
            result = _execute_confirmed_submit_leave(
                state, MagicMock(), self.employee, "127.0.0.1"
            )
        self.assertTrue(result["success"])
        self.assertTrue(result["response_replayed"])
        llm.assert_not_called()
        service.assert_not_called()

    def test_unknown_tool_result_reconciles_persisted_operation(self) -> None:
        persisted = {
            "status": "SUCCEEDED",
            "result_payload": {"application_id": self.application_id},
            "error_code": None,
        }
        result, _model, service, read = self.execute_with(
            AIMessage(content="", tool_calls=[call("submit_leave", self.payload)]),
            service_error=OperationResultUnknownError("lost response"),
            read_side_effect=[OperationNotFoundError("not found"), persisted],
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["response_replayed"])
        self.assertEqual(read.call_count, 2)
        service.assert_called_once()

    def test_processing_failed_and_unknown_never_report_success(self) -> None:
        cases = (
            (OperationInProgressError("processing"), "EXECUTING", "PROCESSING"),
            (OperationPreviouslyFailedError("failed"), "FAILED", "FAILED"),
            (OperationResultUnknownError("unknown"), "UNKNOWN", "UNKNOWN"),
        )
        for error, phase, status in cases:
            with self.subTest(status=status):
                read_effect = (
                    [OperationNotFoundError("not found"), OperationNotFoundError("still absent")]
                    if status == "UNKNOWN"
                    else OperationNotFoundError("not found")
                )
                result, _model, service, _read = self.execute_with(
                    AIMessage(content="", tool_calls=[call("submit_leave", self.payload)]),
                    service_error=error,
                    read_side_effect=read_effect,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["task_phase"], phase)
                self.assertEqual(result["response_operation_status"], status)
                service.assert_called_once()

    def test_run_chat_agent_dispatches_only_after_durable_checkpoint(self) -> None:
        saver = FlushableInMemorySaver()
        prepared = self.prepare(saver=saver)
        lock = MagicMock()
        model = RecordingToolCallingModel(
            AIMessage(content="", tool_calls=[call("submit_leave", self.payload)])
        )
        with (
            patch("app.agents.chat_agent.get_redis_checkpointer", return_value=saver),
            patch("app.agents.chat_agent._TurnBufferedCheckpointSaver", return_value=saver),
            patch("app.agents.chat_agent.get_llm", return_value=model),
            patch("app.agents.chat_agent._read_confirmed_operation", side_effect=OperationNotFoundError("not found")),
            patch("app.tools.leave_tools.create_leave_application_idempotent", return_value=application(self.application_id, self.start)) as service,
        ):
            result = run_chat_agent(
                MagicMock(), self.employee, self.thread_id, "确认提交",
                recognizer=lambda _state: self.fail("confirm must bypass recognition"),
                message_request_id=str(uuid4()), operation_id=self.operation_id,
                task_action="confirm", task_revision=prepared["task_revision"],
                turn_lock=lock, idempotency_session_factory=MagicMock(),
            )
        self.assertTrue(result["success"])
        service.assert_called_once()
        lock.ensure_owned.assert_called()


if __name__ == "__main__":
    unittest.main()
