from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.request_context import reset_request_id, set_request_id
from app.core.security import hash_password
from app.db.mysql import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.user import UserCreate
from app.services.auth_service import (
    AuthenticationReason,
    AuthenticationResult,
    change_password,
)
from app.services.user_service import create_user, reset_user_password, set_user_active_status


def account(user_id: int = 2, *, active: bool = True) -> User:
    now = datetime.now()
    return User(
        id=user_id,
        username=f"employee_{user_id}",
        employee_no=f"EMP{user_id:03d}",
        password_hash=hash_password("Current-Password-123"),
        name="Audit User",
        role="employee",
        department="Testing",
        is_active=active,
        must_change_password=False,
        created_at=now,
        updated_at=now,
    )


def audit_records(db: MagicMock) -> list[AuditLog]:
    return [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], AuditLog)
    ]


class LoginAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = MagicMock()
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.client.close()

    def _login(self, result: AuthenticationResult, password: str = "Never-Store-Me"):
        with patch(
            "app.api.routes.auth.authenticate_user_with_reason",
            return_value=result,
        ):
            return self.client.post(
                "/auth/login",
                headers={"X-Request-ID": "login-audit-request", "User-Agent": "test-agent"},
                json={"username": "employee_2", "password": password},
            )

    def test_successful_login_is_audited(self) -> None:
        response = self._login(
            AuthenticationResult(account(), AuthenticationReason.SUCCESS)
        )
        self.assertEqual(response.status_code, 200)
        record = audit_records(self.db)[0]
        self.assertEqual(record.action, "LOGIN_SUCCESS")
        self.assertEqual(record.request_id, "login-audit-request")
        self.assertEqual(record.detail["reason_code"], "SUCCESS")

    def test_password_error_and_missing_user_are_generic_but_distinct_in_audit(self) -> None:
        for reason, target in (
            (AuthenticationReason.INVALID_PASSWORD, account()),
            (AuthenticationReason.USER_NOT_FOUND, None),
        ):
            with self.subTest(reason=reason.value):
                self.db.reset_mock()
                response = self._login(AuthenticationResult(target, reason))
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"], "Incorrect username or password")
                record = audit_records(self.db)[0]
                self.assertEqual(record.action, "LOGIN_FAILED")
                self.assertEqual(record.detail["reason_code"], reason.value)
                self.assertNotIn("Never-Store-Me", repr(record.detail))

    def test_inactive_login_is_blocked_and_audited(self) -> None:
        response = self._login(
            AuthenticationResult(
                account(active=False),
                AuthenticationReason.USER_INACTIVE,
            )
        )
        self.assertEqual(response.status_code, 401)
        record = audit_records(self.db)[0]
        self.assertEqual(record.action, "LOGIN_BLOCKED")
        self.assertEqual(record.detail["reason_code"], "USER_INACTIVE")

    def test_login_audit_failure_does_not_block_successful_authentication(self) -> None:
        self.db.add.side_effect = RuntimeError("audit storage unavailable")
        response = self._login(
            AuthenticationResult(account(), AuthenticationReason.SUCCESS)
        )
        self.assertEqual(response.status_code, 200)
        self.db.rollback.assert_called_once()


class UserActionAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = set_request_id("user-action-request")

    def tearDown(self) -> None:
        reset_request_id(self.token)

    def test_create_user_is_audited_without_temporary_password(self) -> None:
        db = MagicMock()
        manager = account(8)
        manager.role = "manager"
        db.query.return_value.filter.return_value.first.side_effect = [
            None,
            None,
            None,
            manager,
        ]
        payload = UserCreate.model_validate(
            {
                "username": "employee_20",
                "employee_no": "EMP020",
                "name": "New User",
                "email": "new@example.com",
                "role": "employee",
                "department": "Testing",
                "manager_id": 8,
            }
        )
        with patch(
            "app.services.user_service.generate_temporary_password",
            return_value="Temporary-Secret-123",
        ):
            create_user(db, payload, actor_user_id=1)
        record = audit_records(db)[0]
        self.assertEqual(record.action, "USER_CREATED")
        self.assertEqual(record.request_id, "user-action-request")
        self.assertNotIn("Temporary-Secret-123", repr(record.detail))

    def test_activate_and_deactivate_are_audited(self) -> None:
        target = account()
        for desired, action in ((False, "USER_DEACTIVATED"), (True, "USER_ACTIVATED")):
            db = MagicMock()
            target.is_active = not desired
            db.query.return_value.filter.return_value.first.return_value = target
            set_user_active_status(
                db,
                user_id=target.id,
                is_active=desired,
                actor_user_id=1,
            )
            self.assertEqual(audit_records(db)[0].action, action)

    def test_password_reset_and_change_are_audited_without_secrets(self) -> None:
        target = account()
        reset_db = MagicMock()
        reset_db.query.return_value.filter.return_value.first.return_value = target
        with patch(
            "app.services.user_service.generate_temporary_password",
            return_value="Temporary-Secret-456",
        ):
            reset_user_password(reset_db, user_id=target.id, actor_user_id=1)
        reset_record = audit_records(reset_db)[0]
        self.assertEqual(reset_record.action, "PASSWORD_RESET")
        self.assertNotIn("Temporary-Secret-456", repr(reset_record.detail))

        change_db = MagicMock()
        change_password(
            change_db,
            user=target,
            old_password="Temporary-Secret-456",
            new_password="Personal-Password-789!",
        )
        change_record = audit_records(change_db)[0]
        self.assertEqual(change_record.action, "PASSWORD_CHANGED")
        self.assertNotIn("Personal-Password-789!", repr(change_record.detail))


class LeaveAuditRegressionTests(unittest.TestCase):
    def test_existing_leave_actions_remain_single_source_audits(self) -> None:
        from pathlib import Path

        source = (Path(__file__).parents[1] / "app/services/leave_service.py").read_text(
            encoding="utf-8"
        )
        for action in (
            "CREATE_LEAVE",
            "CANCEL_LEAVE",
            "MANAGER_APPROVE_LEAVE",
            "MANAGER_REJECT_LEAVE",
            "HR_APPROVE_LEAVE",
            "HR_REJECT_LEAVE",
        ):
            self.assertIn(action, source)
        self.assertEqual(source.count("create_audit_log("), 4)


if __name__ == "__main__":
    unittest.main()
