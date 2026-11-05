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
    _execute_confirmed_cancel_leave_via_toolnode,
    project_chat_task_state,
    run_chat_agent,
)
from app.models.user import User
from app.schemas.chat import ChatResponse
from app.services.idempotency_service import OperationResultUnknownError
from app.tools.leave_tools import build_cancel_leave_tool


def user(user_id: int = 80, role: str = "employee") -> User:
    now = datetime.now()
    return User(
        id=user_id,
        username=f"cancel_{role}_{user_id}",
        employee_no=f"CANCEL{user_id:03d}",
        password_hash="not-used",
        name=f"Cancel {role} {user_id}",
        role=role,
        department="Testing",
        manager_id=10 if role == "employee" else None,
        is_active=True,
        must_change_password=False,
        created_at=now,
        updated_at=now,
    )


def application(application_id: str, employee_id: int = 80):
    start = date.today() + timedelta(days=10)
    return SimpleNamespace(
        id=application_id,
        employee_id=employee_id,
        leave_type="annual",
        start_date=start,
        end_date=start + timedelta(days=1),
        leave_days=2,
        status="cancelled",
        manager_status="pending",
        hr_status="pending",
    )


def call(name: str, application_id: str, call_id: str = "call-cancel", **extra):
    return {
        "name": name,
        "args": {"application_id": application_id, **extra},
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


class CancelLeaveToolNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.employee = user()
        self.application_id = str(uuid4())
        self.operation_id = str(uuid4())
        self.thread_id = str(uuid4())
        self.message_request_id = str(uuid4())

    def recognizer(self, _state):
        return {
            "intent": "cancel_leave",
            "extracted_info": {"application_id": self.application_id},
            "reference_index": None,
            "cancel_current_task": False,
            "error": None,
        }

    def prepare(self):
        pending_application = application(self.application_id)
        pending_application.status = "pending"
        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=InMemorySaver(),
            ),
            patch(
                "app.agents.chat_agent.get_cancellable_leave_application",
                return_value=pending_application,
            ) as lookup,
            patch(
                "app.tools.leave_tools.cancel_leave_application_idempotent"
            ) as mutation,
        ):
            result = run_chat_agent(
                MagicMock(name="request-db"),
                self.employee,
                self.thread_id,
                "撤销这条请假",
                recognizer=self.recognizer,
                message_request_id=self.message_request_id,
                operation_id=self.operation_id,
                task_revision=0,
            )
        lookup.assert_called_once()
        mutation.assert_not_called()
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

    def execute_with(self, model_response, *, service_result=None, service_error=None):
        state = self.confirmed_state()
        model = RecordingToolCallingModel(model_response)
        patch_kwargs = (
            {"side_effect": service_error}
            if service_error is not None
            else {
                "return_value": service_result
                or (application(self.application_id), False)
            }
        )
        with (
            patch("app.agents.chat_agent.get_llm", return_value=model),
            patch(
                "app.tools.leave_tools.cancel_leave_application_idempotent",
                **patch_kwargs,
            ) as service,
        ):
            result = _execute_confirmed_cancel_leave_via_toolnode(
                state,
                MagicMock(name="session-factory"),
                self.employee,
                "127.0.0.1",
            )
        return result, model, service

    def test_preparation_requires_explicit_confirmation_and_never_writes(self) -> None:
        result = self.prepare()
        self.assertFalse(result["success"])
        self.assertEqual(result["task_phase"], "AWAITING_CONFIRMATION")
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["active_operation_type"], "CANCEL_LEAVE")
        self.assertEqual(
            result["response_confirmation"]["application_id"],
            self.application_id,
        )
        self.assertEqual(
            result["response_confirmation"]["operation_type"],
            "CANCEL_LEAVE",
        )

    def test_redis_task_projection_restores_only_valid_cancel_confirmation(self) -> None:
        prepared = self.prepare()
        restored = project_chat_task_state(
            prepared,
            user_id=self.employee.id,
            thread_id=self.thread_id,
        )
        self.assertEqual(restored["task_status"], "awaiting_confirmation")
        self.assertEqual(
            restored["confirmation"]["application_id"],
            self.application_id,
        )
        self.assertEqual(restored["confirmation"]["operation_type"], "CANCEL_LEAVE")
        unsafe = project_chat_task_state(
            {**prepared, "confirmation_fingerprint": "0" * 64},
            user_id=self.employee.id,
            thread_id=self.thread_id,
        )
        self.assertEqual(unsafe["task_status"], "unknown")
        self.assertFalse(unsafe["requires_confirmation"])
        self.assertIsNone(unsafe["confirmation"])

    def test_cancel_confirmation_matches_public_chat_response_schema(self) -> None:
        prepared = self.prepare()
        response = ChatResponse(
            success=False,
            thread_id=self.thread_id,
            intent="cancel_leave",
            response=prepared["response"],
            operation_id=self.operation_id,
            task_status="awaiting_confirmation",
            task_revision=prepared["task_revision"],
            requires_confirmation=True,
            code="CHAT_CONFIRMATION_REQUIRED",
            confirmation=prepared["response_confirmation"],
        ).model_dump(mode="json")
        self.assertEqual(response["confirmation"]["operation_type"], "CANCEL_LEAVE")
        self.assertEqual(
            response["confirmation"]["application_id"],
            self.application_id,
        )

    def test_confirmed_cancellation_runs_once_through_toolnode(self) -> None:
        call_id = "call-confirmed-cancel"
        result, model, service = self.execute_with(
            AIMessage(
                content="",
                tool_calls=[call("cancel_leave", self.application_id, call_id)],
            )
        )
        tool_messages = [
            message for message in result["messages"] if isinstance(message, ToolMessage)
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].tool_call_id, call_id)
        self.assertEqual(tool_messages[0].name, "cancel_leave")
        self.assertEqual(model.bound_tools[0].name, "cancel_leave")
        self.assertEqual(len(model.bound_tools), 1)
        self.assertTrue(result["success"])
        self.assertEqual(result["task_phase"], "SUCCEEDED")
        self.assertEqual(result["response_operation_status"], "SUCCEEDED")
        service.assert_called_once()

    def test_run_chat_agent_dispatches_confirmed_cancel_after_checkpoint_barrier(self) -> None:
        saver = FlushableInMemorySaver()
        pending_application = application(self.application_id)
        pending_application.status = "pending"
        with (
            patch("app.agents.chat_agent.get_redis_checkpointer", return_value=saver),
            patch(
                "app.agents.chat_agent.get_cancellable_leave_application",
                return_value=pending_application,
            ),
        ):
            prepared = run_chat_agent(
                MagicMock(),
                self.employee,
                self.thread_id,
                "撤销这条请假",
                recognizer=self.recognizer,
                message_request_id=self.message_request_id,
                operation_id=self.operation_id,
                task_revision=0,
            )

        lock = MagicMock()
        model = RecordingToolCallingModel(
            AIMessage(
                content="",
                tool_calls=[call("cancel_leave", self.application_id)],
            )
        )
        with (
            patch("app.agents.chat_agent.get_redis_checkpointer", return_value=saver),
            patch(
                "app.agents.chat_agent._TurnBufferedCheckpointSaver",
                return_value=saver,
            ),
            patch("app.agents.chat_agent.get_llm", return_value=model),
            patch(
                "app.tools.leave_tools.cancel_leave_application_idempotent",
                return_value=(application(self.application_id), False),
            ) as service,
        ):
            result = run_chat_agent(
                MagicMock(),
                self.employee,
                self.thread_id,
                "确认撤销",
                recognizer=lambda _state: self.fail("confirm must bypass recognition"),
                message_request_id=str(uuid4()),
                operation_id=self.operation_id,
                task_action="confirm",
                task_revision=prepared["task_revision"],
                turn_lock=lock,
                idempotency_session_factory=MagicMock(),
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["intent"], "cancel_leave")
        self.assertEqual(result["response_operation_status"], "SUCCEEDED")
        service.assert_called_once()
        lock.ensure_owned.assert_called()

    def test_cancel_tool_schema_exposes_only_application_id(self) -> None:
        snapshot = self.prepare()["task_payload_snapshot"]
        query_tool = build_cancel_leave_tool(
            MagicMock(),
            self.employee,
            operation_id=self.operation_id,
            confirmed_payload=snapshot,
        )
        schema = query_tool.args_schema.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"application_id"})
        serialized = repr(schema).lower()
        for forbidden in (
            "user_id",
            "role",
            "operation_id",
            "session",
            "confirmation",
            "jwt",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_tool_level_role_and_confirmation_binding_reject_before_service(self) -> None:
        snapshot = self.prepare()["task_payload_snapshot"]
        cases = (
            (user(81, "manager"), self.application_id),
            (self.employee, str(uuid4())),
        )
        for current_user, target in cases:
            with self.subTest(role=current_user.role, target=target):
                query_tool = build_cancel_leave_tool(
                    MagicMock(),
                    current_user,
                    operation_id=self.operation_id,
                    confirmed_payload=snapshot,
                )
                with patch(
                    "app.tools.leave_tools.cancel_leave_application_idempotent"
                ) as service:
                    message = query_tool.invoke(call("cancel_leave", target))
                self.assertFalse(message.artifact["success"])
                service.assert_not_called()

    def test_graph_rbac_rejects_non_employee_before_target_lookup(self) -> None:
        manager = user(82, "manager")
        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=InMemorySaver(),
            ),
            patch("app.agents.chat_agent.get_cancellable_leave_application") as lookup,
        ):
            result = run_chat_agent(
                MagicMock(),
                manager,
                self.thread_id,
                "撤销请假",
                recognizer=self.recognizer,
                message_request_id=self.message_request_id,
                operation_id=self.operation_id,
                task_revision=0,
            )
        self.assertEqual(result["error"], "permission_denied")
        lookup.assert_not_called()

    def test_invalid_tool_calls_never_execute_service(self) -> None:
        cases = {
            "no_call": AIMessage(content="直接回答"),
            "wrong_name": AIMessage(
                content="",
                tool_calls=[call("query_my_leave", self.application_id)],
            ),
            "wrong_target": AIMessage(
                content="",
                tool_calls=[call("cancel_leave", str(uuid4()))],
            ),
            "extra_identity": AIMessage(
                content="",
                tool_calls=[
                    call("cancel_leave", self.application_id, user_id=999)
                ],
            ),
            "multiple": AIMessage(
                content="",
                tool_calls=[
                    call("cancel_leave", self.application_id, "call-one"),
                    call("cancel_leave", self.application_id, "call-two"),
                ],
            ),
            "missing_call_id": AIMessage(
                content="",
                tool_calls=[call("cancel_leave", self.application_id, "")],
            ),
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                result, _model, service = self.execute_with(response)
                self.assertFalse(result["success"])
                self.assertEqual(result["task_phase"], "FAILED")
                service.assert_not_called()

    def test_confirmation_tampering_and_user_cancel_never_execute_tool(self) -> None:
        prepared = self.prepare()
        tampered = {
            **prepared,
            "task_payload_snapshot": {
                **prepared["task_payload_snapshot"],
                "application_id": str(uuid4()),
            },
            "request_operation_id": self.operation_id,
            "request_task_revision": prepared["task_revision"],
        }
        rejected = _confirm_submit_leave_node(tampered)
        self.assertEqual(rejected["code"], "CHAT_CONFIRMATION_INVALIDATED")

        cancelled = _cancel_submit_leave_task_node(
            {
                **prepared,
                "request_operation_id": self.operation_id,
                "request_task_revision": prepared["task_revision"],
            }
        )
        self.assertTrue(cancelled["success"])
        self.assertEqual(cancelled["task_phase"], "IDLE")
        self.assertIn("没有撤销", cancelled["response"])

    def test_unknown_service_result_is_never_reported_as_success(self) -> None:
        result, _model, service = self.execute_with(
            AIMessage(
                content="",
                tool_calls=[call("cancel_leave", self.application_id)],
            ),
            service_error=OperationResultUnknownError("unknown"),
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["task_phase"], "UNKNOWN")
        self.assertEqual(result["response_operation_status"], "UNKNOWN")
        self.assertIn("无法确认", result["response"])
        service.assert_called_once()

    def test_replayed_success_is_reported_without_second_tool_invocation(self) -> None:
        result, _model, service = self.execute_with(
            AIMessage(
                content="",
                tool_calls=[call("cancel_leave", self.application_id)],
            ),
            service_result=(application(self.application_id), True),
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["response_replayed"])
        service.assert_called_once()


if __name__ == "__main__":
    unittest.main()
