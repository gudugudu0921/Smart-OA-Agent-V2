from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.core.security import hash_password, verify_password
from app.models.user import User
from app.services.auth_service import PasswordChangeError, change_password
from app.services.user_service import UserValidationError, reset_user_password


def account(user_id: int = 2, role: str = "employee") -> User:
    return User(
        id=user_id,
        username=f"{role}_{user_id}",
        employee_no=f"EMP{user_id:03d}",
        password_hash=hash_password("Old-Password-123"),
        name="Password Test",
        role=role,
        department="Testing",
        is_active=True,
        must_change_password=True,
    )


class PasswordManagementTests(unittest.TestCase):
    def test_correct_current_password_changes_password_and_state(self) -> None:
        db = MagicMock()
        target = account()
        changed_at = datetime(2026, 9, 28, 12, 0, 0)
        old_hash = target.password_hash
        with patch(
            "app.services.auth_service.utc_now_naive",
            return_value=changed_at,
        ):
            result = change_password(
                db,
                user=target,
                old_password="Old-Password-123",
                new_password="New-Password-456!",
            )

        self.assertIs(result, target)
        self.assertNotEqual(result.password_hash, old_hash)
        self.assertTrue(verify_password("New-Password-456!", result.password_hash))
        self.assertFalse(result.must_change_password)
        self.assertEqual(result.password_changed_at, changed_at)
        db.commit.assert_called_once()

    def test_wrong_current_password_is_rejected(self) -> None:
        db = MagicMock()
        with self.assertRaisesRegex(PasswordChangeError, "CURRENT_PASSWORD_INCORRECT"):
            change_password(
                db,
                user=account(),
                old_password="Wrong-Password-123",
                new_password="New-Password-456!",
            )
        db.commit.assert_not_called()

    def test_new_password_must_differ(self) -> None:
        db = MagicMock()
        with self.assertRaisesRegex(PasswordChangeError, "NEW_PASSWORD_MUST_DIFFER"):
            change_password(
                db,
                user=account(),
                old_password="Old-Password-123",
                new_password="Old-Password-123",
            )

    def test_weak_passwords_are_rejected(self) -> None:
        for password in ("123456", "password", "password1"):
            with self.subTest(password=password):
                with self.assertRaisesRegex(PasswordChangeError, "PASSWORD_"):
                    change_password(
                        MagicMock(),
                        user=account(),
                        old_password="Old-Password-123",
                        new_password=password,
                    )

    def test_admin_reset_returns_one_time_password_and_updates_state(self) -> None:
        db = MagicMock()
        target = account()
        db.query.return_value.filter.return_value.first.return_value = target
        changed_at = datetime(2026, 9, 28, 12, 30, 0)
        with (
            patch(
                "app.services.user_service.generate_temporary_password",
                return_value="Temporary-Password-789",
            ),
            patch(
                "app.services.user_service.utc_now_naive",
                return_value=changed_at,
            ),
        ):
            result, temporary_password = reset_user_password(
                db,
                user_id=target.id,
                actor_user_id=1,
            )

        self.assertIs(result, target)
        self.assertEqual(temporary_password, "Temporary-Password-789")
        self.assertNotEqual(result.password_hash, temporary_password)
        self.assertTrue(verify_password(temporary_password, result.password_hash))
        self.assertTrue(result.must_change_password)
        self.assertEqual(result.password_changed_at, changed_at)

    def test_admin_cannot_reset_own_password(self) -> None:
        db = MagicMock()
        admin = account(user_id=1, role="admin")
        db.query.return_value.filter.return_value.first.return_value = admin
        with self.assertRaisesRegex(UserValidationError, "CANNOT_RESET_SELF_PASSWORD"):
            reset_user_password(db, user_id=1, actor_user_id=1)


if __name__ == "__main__":
    unittest.main()
