from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.core.security import verify_password
from app.models.user import User
from app.schemas.user import UserCreate
from app.services.user_service import (
    UserConflictError,
    UserNotFoundError,
    UserValidationError,
    create_user,
    set_user_active_status,
)


def user(user_id: int, role: str, *, active: bool = True) -> User:
    value = User(
        id=user_id,
        username=f"{role}_{user_id}",
        employee_no=f"EMP{user_id:03d}",
        password_hash="unused",
        name=f"User {user_id}",
        role=role,
        department="Testing",
        is_active=active,
        must_change_password=False,
    )
    return value


def employee_payload(**changes: object) -> UserCreate:
    data = {
        "username": "employee_020",
        "employee_no": "EMP020",
        "name": "测试员工",
        "email": "employee20@example.com",
        "role": "employee",
        "department": "测试部",
        "position": "测试工程师",
        "manager_id": 2,
    }
    data.update(changes)
    return UserCreate.model_validate(data)


class UserServiceTests(unittest.TestCase):
    def test_create_employee_hashes_unique_temporary_password(self) -> None:
        db = MagicMock()
        manager = user(2, "manager")
        db.query.return_value.filter.return_value.first.side_effect = [
            None,
            None,
            None,
            manager,
        ]

        with patch(
            "app.services.user_service.generate_temporary_password",
            return_value="OneTime-Test-Password",
        ):
            created, temporary_password = create_user(db, employee_payload())

        self.assertEqual(temporary_password, "OneTime-Test-Password")
        self.assertNotEqual(created.password_hash, temporary_password)
        self.assertTrue(verify_password(temporary_password, created.password_hash))
        self.assertTrue(created.is_active)
        self.assertTrue(created.must_change_password)
        self.assertIsNone(created.password_changed_at)
        self.assertEqual(created.manager_id, manager.id)
        db.add.assert_called_once_with(created)
        db.commit.assert_called_once()

    def test_duplicate_username_is_rejected(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user(9, "employee")
        with self.assertRaisesRegex(UserConflictError, "USERNAME_ALREADY_EXISTS"):
            create_user(db, employee_payload())

    def test_duplicate_employee_number_is_rejected(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            None,
            user(9, "employee"),
        ]
        with self.assertRaisesRegex(UserConflictError, "EMPLOYEE_NO_ALREADY_EXISTS"):
            create_user(db, employee_payload())

    def test_duplicate_email_is_rejected(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            None,
            None,
            user(9, "employee"),
        ]
        with self.assertRaisesRegex(UserConflictError, "EMAIL_ALREADY_EXISTS"):
            create_user(db, employee_payload())

    def test_employee_manager_must_exist(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, None, None, None]
        with self.assertRaisesRegex(UserValidationError, "MANAGER_NOT_FOUND"):
            create_user(db, employee_payload())

    def test_employee_manager_must_have_manager_role(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            None,
            None,
            None,
            user(3, "employee"),
        ]
        with self.assertRaisesRegex(UserValidationError, "MANAGER_ROLE_REQUIRED"):
            create_user(db, employee_payload(manager_id=3))

    def test_employee_manager_must_be_active(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            None,
            None,
            None,
            user(2, "manager", active=False),
        ]
        with self.assertRaisesRegex(UserValidationError, "MANAGER_INACTIVE"):
            create_user(db, employee_payload())

    def test_employee_requires_manager(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, None, None]
        with self.assertRaisesRegex(UserValidationError, "EMPLOYEE_MANAGER_REQUIRED"):
            create_user(db, employee_payload(manager_id=None))

    def test_non_employee_cannot_have_manager(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, None, None]
        with self.assertRaisesRegex(UserValidationError, "MANAGER_ID_NOT_ALLOWED_FOR_ROLE"):
            create_user(db, employee_payload(role="hr", manager_id=2))

    def test_admin_can_disable_and_enable_user(self) -> None:
        db = MagicMock()
        target = user(3, "employee")
        db.query.return_value.filter.return_value.first.return_value = target
        disabled = set_user_active_status(
            db,
            user_id=target.id,
            is_active=False,
            actor_user_id=1,
        )
        self.assertFalse(disabled.is_active)

        db.reset_mock()
        db.query.return_value.filter.return_value.first.return_value = target
        enabled = set_user_active_status(
            db,
            user_id=target.id,
            is_active=True,
            actor_user_id=1,
        )
        self.assertTrue(enabled.is_active)

    def test_admin_cannot_disable_self(self) -> None:
        db = MagicMock()
        admin = user(1, "admin")
        db.query.return_value.filter.return_value.first.return_value = admin
        with self.assertRaisesRegex(UserValidationError, "CANNOT_DISABLE_SELF"):
            set_user_active_status(
                db,
                user_id=admin.id,
                is_active=False,
                actor_user_id=admin.id,
            )

    def test_missing_status_target_is_rejected(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with self.assertRaisesRegex(UserNotFoundError, "USER_NOT_FOUND"):
            set_user_active_status(db, user_id=999, is_active=False, actor_user_id=1)


if __name__ == "__main__":
    unittest.main()
