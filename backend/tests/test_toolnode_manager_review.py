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
    _execute_confirmed_manager_review_via_toolnode,
    project_chat_task_state,
    run_chat_agent,
)
from app.models.user import User
from app.schemas.chat import ChatResponse
from app.services.idempotency_service import OperationResultUnknownError
from app.services.leave_service import LeaveNotFoundError
from app.tools.approval_tools import build_manager_review_tool


def user(user_id: int = 90, role: str = "manager") -> User:
    now = datetime.now()
    return User(
        id=user_id,
        username=f"manager_review_{role}_{user_id}",
        employee_no=f"MREVIEW{user_id:03d}",
        password_hash="not-used",
        name=f"Manager Review {role} {user_id}",
        role=role,
        department="Testing",
        manager_id=None,
        is_active=True,
        must_change_password=False,
        created_at=now,
        updated_at=now,
    )


def employee(user_id: int = 91) -> User:
    item = user(user_id, "employee")
    item.name = "Review Target"
    item.employee_no = "TARGET091"
    item.manager_id = 90
    return item


def application(
    application_id: str,
    *,
    decision: str = "approved",
):
    start = date.today() + timedelta(days=10)
    return SimpleNamespace(
        id=application_id,
        employee_id=91,
        leave_type="annual",
        start_date=start,
        end_date=start + timedelta(days=1),
        leave_days=2,
        reason="manager review test",
        status="pending" if decision == "approved" else "rejected",
        manager_status=decision,
        manager_id=90,
        manager_comment="looks good",
        hr_status="pending",
    )


def call(
    name: str,
    application_id: str,
    decision: str = "approved",
    comment: str | None = "looks good",
    call_id: str = "call-manager-review",
    **extra,
):
    return {
        "name": name,
        "args": {
            "application_id": application_id,
            "decision": decision,
            "comment": comment,
            **extra,
        },
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


class ManagerReviewToolNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = user()
        self.employee = employee()
        self.application_id = str(uuid4())
        self.operation_id = str(uuid4())
        self.thread_id = str(uuid4())
        self.message_request_id = str(uuid4())
        self.decision = "approved"
        self.comment = "looks good"

    def recognizer(self, _state):
        return {
            "intent": "manager_review",
            "extracted_info": {
                "application_id": self.application_id,
                "decision": self.decision,
                "comment": self.comment,
            },
            "reference_index": None,
            "cancel_current_task": False,
            "error": None,
        }

    def request_db(self):
        db = MagicMock(name="request-db")
        db.get.return_value = self.employee
        return db

    def prepare(self):
        pending = application(self.application_id)
        pending.manager_status = "pending"
        db = self.request_db()
        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=InMemorySaver(),
            ),
            patch(
                "app.agents.chat_agent.get_manager_reviewable_leave_application",
                return_value=pending,
            ) as lookup,
            patch(
                "app.tools.approval_tools.review_leave_by_manager_idempotent"
            ) as mutation,
        ):
            result = run_chat_agent(
                db,
                self.manager,
                self.thread_id,
                "同意这条申请",
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
                "app.tools.approval_tools.review_leave_by_manager_idempotent",
                **patch_kwargs,
            ) as service,
        ):
            result = _execute_confirmed_manager_review_via_toolnode(
                state,
                MagicMock(name="session-factory"),
                self.manager,
                "127.0.0.1",
            )
        return result, model, service

    def test_preparation_requires_confirmation_and_exposes_target_summary(self) -> None:
        result = self.prepare()
        confirmation = result["response_confirmation"]
        self.assertEqual(result["task_phase"], "AWAITING_CONFIRMATION")
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["active_operation_type"], "MANAGER_REVIEW")
        self.assertEqual(confirmation["operation_type"], "MANAGER_REVIEW")
        self.assertEqual(confirmation["application_id"], self.application_id)
        self.assertEqual(confirmation["employee_name"], self.employee.name)
        self.assertEqual(confirmation["decision"], "approved")

    def test_manager_confirmation_matches_public_response_schema(self) -> None:
        prepared = self.prepare()
        response = ChatResponse(
            success=False,
            thread_id=self.thread_id,
            intent="manager_review",
            response=prepared["response"],
            operation_id=self.operation_id,
            task_status="awaiting_confirmation",
            task_revision=prepared["task_revision"],
            requires_confirmation=True,
            code="CHAT_CONFIRMATION_REQUIRED",
            confirmation=prepared["response_confirmation"],
        ).model_dump(mode="json")
        self.assertEqual(response["confirmation"]["operation_type"], "MANAGER_REVIEW")

    def test_projection_restores_only_untampered_manager_confirmation(self) -> None:
        prepared = self.prepare()
        restored = project_chat_task_state(
            prepared,
            user_id=self.manager.id,
            thread_id=self.thread_id,
        )
        self.assertEqual(restored["task_status"], "awaiting_confirmation")
        self.assertEqual(restored["confirmation"]["decision"], "approved")
        unsafe = project_chat_task_state(
            {**prepared, "confirmation_fingerprint": "0" * 64},
            user_id=self.manager.id,
            thread_id=self.thread_id,
        )
        self.assertEqual(unsafe["task_status"], "unknown")
        self.assertFalse(unsafe["requires_confirmation"])

    def test_approved_and_rejected_decisions_run_once_through_toolnode(self) -> None:
        for decision in ("approved", "rejected"):
            with self.subTest(decision=decision):
                self.decision = decision
                self.comment = "decision comment"
                call_id = f"call-{decision}"
                result, model, service = self.execute_with(
                    AIMessage(
                        content="",
                        tool_calls=[
                            call(
                                "manager_review",
                                self.application_id,
                                decision,
                                self.comment,
                                call_id,
                            )
                        ],
                    ),
                    service_result=(
                        application(self.application_id, decision=decision),
                        False,
                    ),
                )
                tool_messages = [
                    message
                    for message in result["messages"]
                    if isinstance(message, ToolMessage)
                ]
                self.assertEqual(len(tool_messages), 1)
                self.assertEqual(tool_messages[0].tool_call_id, call_id)
                self.assertEqual(tool_messages[0].name, "manager_review")
                self.assertEqual(model.bound_tools[0].name, "manager_review")
                self.assertTrue(result["success"])
                self.assertIn(
                    "HR" if decision == "approved" else "拒绝",
                    result["response"],
                )
                service.assert_called_once()

    def test_run_chat_agent_dispatches_after_durable_checkpoint_barrier(self) -> None:
        saver = FlushableInMemorySaver()
        pending = application(self.application_id)
        pending.manager_status = "pending"
        db = self.request_db()
        with (
            patch("app.agents.chat_agent.get_redis_checkpointer", return_value=saver),
            patch(
                "app.agents.chat_agent.get_manager_reviewable_leave_application",
                return_value=pending,
            ),
        ):
            prepared = run_chat_agent(
                db,
                self.manager,
                self.thread_id,
                "同意这条申请",
                recognizer=self.recognizer,
                message_request_id=self.message_request_id,
                operation_id=self.operation_id,
                task_revision=0,
            )

        lock = MagicMock()
        model = RecordingToolCallingModel(
            AIMessage(
                content="",
                tool_calls=[
                    call("manager_review", self.application_id, "approved", self.comment)
                ],
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
                "app.tools.approval_tools.review_leave_by_manager_idempotent",
                return_value=(application(self.application_id), False),
            ) as service,
        ):
            result = run_chat_agent(
                db,
                self.manager,
                self.thread_id,
                "确认审批",
                recognizer=lambda _state: self.fail("confirm must bypass recognition"),
                message_request_id=str(uuid4()),
                operation_id=self.operation_id,
                task_action="confirm",
                task_revision=prepared["task_revision"],
                turn_lock=lock,
                idempotency_session_factory=MagicMock(),
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["intent"], "manager_review")
        service.assert_called_once()
        lock.ensure_owned.assert_called()

    def test_tool_schema_contains_only_review_business_arguments(self) -> None:
        snapshot = self.prepare()["task_payload_snapshot"]
        manager_tool = build_manager_review_tool(
            MagicMock(),
            self.manager,
            operation_id=self.operation_id,
            confirmed_payload=snapshot,
        )
        schema = manager_tool.args_schema.model_json_schema()
        self.assertEqual(
            set(schema["properties"]),
            {"application_id", "decision", "comment"},
        )
        serialized = repr(schema).lower()
        for forbidden in (
            "manager_id",
            "user_id",
            "operation_id",
            "session",
            "fingerprint",
            "revision",
            "jwt",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_tool_defense_rejects_role_target_decision_and_comment_mismatch(self) -> None:
        snapshot = self.prepare()["task_payload_snapshot"]
        cases = (
            (user(92, "employee"), self.application_id, "approved", self.comment),
            (self.manager, str(uuid4()), "approved", self.comment),
            (self.manager, self.application_id, "rejected", self.comment),
            (self.manager, self.application_id, "approved", "changed"),
        )
        for actor, target, decision, comment in cases:
            with self.subTest(actor=actor.role, target=target, decision=decision):
                manager_tool = build_manager_review_tool(
                    MagicMock(),
                    actor,
                    operation_id=self.operation_id,
                    confirmed_payload=snapshot,
                )
                with patch(
                    "app.tools.approval_tools.review_leave_by_manager_idempotent"
                ) as service:
                    message = manager_tool.invoke(
                        call("manager_review", target, decision, comment)
                    )
                self.assertFalse(message.artifact["success"])
                service.assert_not_called()

    def test_graph_rbac_and_assignment_failure_never_prepare_write(self) -> None:
        employee_actor = user(93, "employee")
        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=InMemorySaver(),
            ),
            patch(
                "app.agents.chat_agent.get_manager_reviewable_leave_application"
            ) as lookup,
        ):
            result = run_chat_agent(
                self.request_db(),
                employee_actor,
                self.thread_id,
                "同意申请",
                recognizer=self.recognizer,
                message_request_id=self.message_request_id,
                operation_id=self.operation_id,
                task_revision=0,
            )
        self.assertEqual(result["error"], "permission_denied")
        lookup.assert_not_called()

        with (
            patch(
                "app.agents.chat_agent.get_redis_checkpointer",
                return_value=InMemorySaver(),
            ),
            patch(
                "app.agents.chat_agent.get_manager_reviewable_leave_application",
                side_effect=LeaveNotFoundError("hidden"),
            ),
            patch(
                "app.tools.approval_tools.review_leave_by_manager_idempotent"
            ) as service,
        ):
            result = run_chat_agent(
                self.request_db(),
                self.manager,
                self.thread_id,
                "同意申请",
                recognizer=self.recognizer,
                message_request_id=self.message_request_id,
                operation_id=self.operation_id,
                task_revision=0,
            )
        self.assertEqual(result["error"], "not_found")
        service.assert_not_called()

    def test_invalid_tool_calls_never_execute_service(self) -> None:
        cases = {
            "no_call": AIMessage(content="直接回答"),
            "wrong_name": AIMessage(
                content="",
                tool_calls=[call("hr_review", self.application_id)],
            ),
            "wrong_target": AIMessage(
                content="",
                tool_calls=[call("manager_review", str(uuid4()))],
            ),
            "wrong_decision": AIMessage(
                content="",
                tool_calls=[call("manager_review", self.application_id, "rejected")],
            ),
            "wrong_comment": AIMessage(
                content="",
                tool_calls=[
                    call("manager_review", self.application_id, comment="changed")
                ],
            ),
            "extra_identity": AIMessage(
                content="",
                tool_calls=[
                    call("manager_review", self.application_id, manager_id=90)
                ],
            ),
            "multiple": AIMessage(
                content="",
                tool_calls=[
                    call("manager_review", self.application_id, call_id="call-one"),
                    call("manager_review", self.application_id, call_id="call-two"),
                ],
            ),
            "missing_call_id": AIMessage(
                content="",
                tool_calls=[call("manager_review", self.application_id, call_id="")],
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
                "decision": "rejected",
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
        self.assertIn("没有审批", cancelled["response"])

    def test_unknown_result_is_not_reported_as_success_and_replay_is_stable(self) -> None:
        unknown, _model, service = self.execute_with(
            AIMessage(
                content="",
                tool_calls=[call("manager_review", self.application_id)],
            ),
            service_error=OperationResultUnknownError("unknown"),
        )
        self.assertFalse(unknown["success"])
        self.assertEqual(unknown["task_phase"], "UNKNOWN")
        self.assertEqual(unknown["response_operation_status"], "UNKNOWN")
        service.assert_called_once()

        replay, _model, service = self.execute_with(
            AIMessage(
                content="",
                tool_calls=[call("manager_review", self.application_id)],
            ),
            service_result=(application(self.application_id), True),
        )
        self.assertTrue(replay["success"])
        self.assertTrue(replay["response_replayed"])
        service.assert_called_once()


if __name__ == "__main__":
    unittest.main()
