from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.dependencies import get_current_user
from app.db.mysql import get_db
from app.main import app
from app.models.user import User


def response_user(user_id: int, role: str) -> User:
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
        must_change_password=role == "employee",
        last_login_at=None,
        password_changed_at=None,
        created_at=now,
        updated_at=now,
    )


class UserRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: object()

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.client.close()

    def authenticate_as(self, role: str, user_id: int = 1) -> None:
        current = response_user(user_id, role)
        app.dependency_overrides[get_current_user] = lambda: current

    @staticmethod
    def payload() -> dict:
        return {
            "username": "employee_020",
            "employee_no": "EMP020",
            "name": "测试员工",
            "email": "employee20@example.com",
            "role": "employee",
            "department": "测试部",
            "position": "测试工程师",
            "manager_id": 2,
        }

    def test_admin_can_create_user(self) -> None:
        self.authenticate_as("admin")
        created = response_user(20, "employee")
        created.manager_id = 2
        with patch(
            "app.api.routes.users.create_user",
            return_value=(created, "OneTime-Test-Password"),
        ):
            response = self.client.post("/users/", json=self.payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["temporary_password"], "OneTime-Test-Password")
        self.assertTrue(response.json()["user"]["must_change_password"])
        self.assertNotIn("password_hash", response.json()["user"])

    def test_non_admin_roles_cannot_create_user(self) -> None:
        for role in ("employee", "manager", "hr"):
            with self.subTest(role=role):
                self.authenticate_as(role)
                response = self.client.post("/users/", json=self.payload())
                self.assertEqual(response.status_code, 403)

    def test_invalid_role_is_rejected(self) -> None:
        self.authenticate_as("admin")
        payload = self.payload()
        payload["role"] = "superuser"
        response = self.client.post("/users/", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_admin_can_update_status(self) -> None:
        self.authenticate_as("admin")
        target = response_user(20, "employee")
        target.is_active = False
        with patch(
            "app.api.routes.users.set_user_active_status",
            return_value=target,
        ):
            response = self.client.patch("/users/20/status", json={"is_active": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_active"])

    def test_employee_cannot_update_status(self) -> None:
        self.authenticate_as("employee")
        response = self.client.patch("/users/20/status", json={"is_active": False})
        self.assertEqual(response.status_code, 403)

    def test_original_auth_and_leave_routes_are_registered(self) -> None:
        paths = set(app.openapi()["paths"])
        self.assertIn("/auth/login", paths)
        self.assertIn("/leave/", paths)
        self.assertIn("/leave/{application_id}/manager-review", paths)
        self.assertIn("/leave/{application_id}/hr-review", paths)


if __name__ == "__main__":
    unittest.main()
