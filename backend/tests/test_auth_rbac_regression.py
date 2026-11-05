from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.core.dependencies import require_roles
from app.core.security import hash_password
from app.models.user import User
from app.services.auth_service import authenticate_user


def account(role: str, *, active: bool = True) -> User:
    return User(
        id=1,
        username=f"{role}_user",
        employee_no="TEST001",
        password_hash=hash_password("Correct-Test-Password"),
        name="Test User",
        role=role,
        department="Testing",
        is_active=active,
        must_change_password=False,
    )


class AuthAndRbacRegressionTests(unittest.TestCase):
    def test_inactive_user_cannot_log_in(self) -> None:
        db = MagicMock()
        with patch(
            "app.services.auth_service.get_user_by_username",
            return_value=account("employee", active=False),
        ):
            result = authenticate_user(
                db,
                "employee_user",
                "Correct-Test-Password",
            )
        self.assertIsNone(result)
        db.commit.assert_not_called()

    def test_active_user_authentication_is_unchanged(self) -> None:
        expected = account("employee")
        db = MagicMock()
        with patch(
            "app.services.auth_service.get_user_by_username",
            return_value=expected,
        ):
            result = authenticate_user(
                db,
                "employee_user",
                "Correct-Test-Password",
            )
        self.assertIs(result, expected)
        self.assertIsNotNone(expected.last_login_at)
        db.commit.assert_called_once()

    def test_wrong_password_does_not_update_last_login(self) -> None:
        expected = account("employee")
        db = MagicMock()
        with patch(
            "app.services.auth_service.get_user_by_username",
            return_value=expected,
        ):
            result = authenticate_user(db, "employee_user", "Wrong-Password")
        self.assertIsNone(result)
        self.assertIsNone(expected.last_login_at)
        db.commit.assert_not_called()

    def test_existing_four_role_checks_are_unchanged(self) -> None:
        for role in ("employee", "manager", "hr", "admin"):
            with self.subTest(role=role):
                checker = require_roles(role)
                current = account(role)
                self.assertIs(checker(current), current)
                with self.assertRaises(HTTPException) as raised:
                    checker(account("different"))
                self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
