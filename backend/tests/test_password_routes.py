from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.dependencies import get_current_user
from app.db.mysql import get_db
from app.main import app
from app.models.user import User
from app.services.auth_service import AuthenticationReason, AuthenticationResult


def response_user(
    user_id: int,
    role: str,
    *,
    must_change: bool = False,
) -> User:
    now = datetime.now()
    return User(
        id=user_id,
        username=f"{role}_{user_id}",
        employee_no=f"EMP{user_id:03d}",
        password_hash="not-returned",
        name=f"User {user_id}",
        email=None,
        role=role,
        department="Testing",
        position=None,
        manager_id=None,
        is_active=True,
        must_change_password=must_change,
        last_login_at=now,
        password_changed_at=None,
        created_at=now,
        updated_at=now,
    )


class PasswordRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: object()

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.client.close()

    def authenticate(self, user: User) -> None:
        app.dependency_overrides[get_current_user] = lambda: user

    def test_forced_user_can_log_in(self) -> None:
        target = response_user(2, "employee", must_change=True)
        with patch(
            "app.api.routes.auth.authenticate_user_with_reason",
            return_value=AuthenticationResult(target, AuthenticationReason.SUCCESS),
        ):
            response = self.client.post(
                "/auth/login",
                json={"username": target.username, "password": "Temporary-Password-123"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_token", response.json())

    def test_forced_user_can_read_self_and_change_password(self) -> None:
        target = response_user(2, "employee", must_change=True)
        self.authenticate(target)
        self.assertEqual(self.client.get("/users/me").status_code, 200)
        with patch("app.api.routes.auth.change_password", return_value=target):
            response = self.client.post(
                "/auth/change-password",
                json={
                    "old_password": "Temporary-Password-123",
                    "new_password": "Personal-Password-456",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password", response.json())

    def test_forced_user_cannot_access_leave_or_chat(self) -> None:
        self.authenticate(response_user(2, "employee", must_change=True))
        leave_response = self.client.get("/leave/my")
        chat_response = self.client.get("/chat/sessions")
        self.assertEqual(leave_response.status_code, 403)
        self.assertEqual(chat_response.status_code, 403)
        self.assertEqual(
            leave_response.json()["detail"]["code"],
            "PASSWORD_CHANGE_REQUIRED",
        )

    def test_user_can_access_business_after_password_change(self) -> None:
        self.authenticate(response_user(2, "employee", must_change=False))
        with patch(
            "app.api.routes.leave.get_my_leave_applications",
            return_value=[],
        ):
            response = self.client.get("/leave/my")
        self.assertEqual(response.status_code, 200)

    def test_admin_can_reset_password_once(self) -> None:
        self.authenticate(response_user(1, "admin"))
        target = response_user(2, "employee", must_change=True)
        with patch(
            "app.api.routes.users.reset_user_password",
            return_value=(target, "Temporary-Password-789"),
        ):
            response = self.client.post("/users/2/reset-password")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["temporary_password"], "Temporary-Password-789")
        self.assertNotIn("password_hash", response.json())

    def test_non_admin_roles_cannot_reset_password(self) -> None:
        for role in ("employee", "manager", "hr"):
            with self.subTest(role=role):
                self.authenticate(response_user(3, role))
                response = self.client.post("/users/2/reset-password")
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
