from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import IntegrityError

from app.core.security import verify_password
from app.db.create_admin import (
    BootstrapAdminError,
    BootstrapAdminInput,
    create_bootstrap_admin,
    main,
)
from app.models.user import User


def payload(**changes: str) -> BootstrapAdminInput:
    values = {
        "username": "bootstrap_admin",
        "employee_no": "BOOTSTRAP001",
        "name": "Bootstrap Administrator",
        "email": "bootstrap@example.com",
    }
    values.update(changes)
    return BootstrapAdminInput(**values)


def existing_user(*, role: str = "employee") -> User:
    return User(
        id=1,
        username="existing",
        employee_no="EXISTING001",
        password_hash="unused",
        name="Existing User",
        email="existing@example.com",
        role=role,
        department="Testing",
        is_active=True,
        must_change_password=False,
    )


def empty_database() -> MagicMock:
    db = MagicMock()
    db.query.return_value.order_by.return_value.with_for_update.return_value.all.return_value = []
    return db


class BootstrapAdminTests(unittest.TestCase):
    def test_fresh_install_creates_active_admin_with_one_time_password(self) -> None:
        db = empty_database()
        with patch(
            "app.db.create_admin.generate_temporary_password",
            return_value="Bootstrap-One-Time-Password!",
        ):
            administrator, temporary_password = create_bootstrap_admin(db, payload())

        self.assertEqual(temporary_password, "Bootstrap-One-Time-Password!")
        self.assertEqual(administrator.role, "admin")
        self.assertTrue(administrator.is_active)
        self.assertTrue(administrator.must_change_password)
        self.assertIsNone(administrator.last_login_at)
        self.assertIsNone(administrator.password_changed_at)
        self.assertNotEqual(administrator.password_hash, temporary_password)
        self.assertTrue(verify_password(temporary_password, administrator.password_hash))
        db.add.assert_called_once_with(administrator)
        db.query.return_value.order_by.return_value.with_for_update.assert_called_once_with()
        db.flush.assert_called_once_with()
        db.commit.assert_called_once_with()
        db.rollback.assert_not_called()

    def test_existing_admin_is_rejected(self) -> None:
        db = empty_database()
        db.query.return_value.order_by.return_value.with_for_update.return_value.all.return_value = [
            existing_user(role="admin")
        ]

        with self.assertRaisesRegex(BootstrapAdminError, "ADMIN_ALREADY_EXISTS"):
            create_bootstrap_admin(db, payload())

        db.add.assert_not_called()
        db.commit.assert_not_called()
        db.rollback.assert_called_once_with()

    def test_cli_prints_temporary_password_once_after_success(self) -> None:
        db = MagicMock()
        administrator = existing_user(role="admin")
        administrator.username = "bootstrap_admin"
        output = StringIO()
        with (
            patch("app.db.create_admin.SessionLocal", return_value=db),
            patch(
                "app.db.create_admin.create_bootstrap_admin",
                return_value=(administrator, "One-Time-CLI-Secret!"),
            ),
            redirect_stdout(output),
        ):
            result = main(
                [
                    "--username",
                    "bootstrap_admin",
                    "--employee-no",
                    "BOOTSTRAP001",
                    "--name",
                    "Bootstrap Administrator",
                    "--email",
                    "bootstrap@example.com",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().count("One-Time-CLI-Secret!"), 1)
        db.close.assert_called_once_with()

    def test_cli_refusal_does_not_print_a_password(self) -> None:
        db = MagicMock()
        output = StringIO()
        errors = StringIO()
        with (
            patch("app.db.create_admin.SessionLocal", return_value=db),
            patch(
                "app.db.create_admin.create_bootstrap_admin",
                side_effect=BootstrapAdminError("ADMIN_ALREADY_EXISTS"),
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = main(
                [
                    "--username",
                    "bootstrap_admin",
                    "--employee-no",
                    "BOOTSTRAP001",
                    "--name",
                    "Bootstrap Administrator",
                    "--email",
                    "bootstrap@example.com",
                ]
            )

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue().strip(), "ADMIN_ALREADY_EXISTS")
        db.close.assert_called_once_with()

    def test_nonempty_database_without_admin_is_rejected(self) -> None:
        db = empty_database()
        db.query.return_value.order_by.return_value.with_for_update.return_value.all.return_value = [
            existing_user()
        ]

        with self.assertRaisesRegex(BootstrapAdminError, "NOT_FRESH_INSTALL"):
            create_bootstrap_admin(db, payload())

        db.add.assert_not_called()
        db.commit.assert_not_called()
        db.rollback.assert_called_once_with()

    def test_invalid_identity_is_rejected_before_insert(self) -> None:
        db = empty_database()

        with self.assertRaisesRegex(BootstrapAdminError, "BOOTSTRAP_INPUT_INVALID"):
            create_bootstrap_admin(db, payload(username="invalid username"))

        db.add.assert_not_called()
        db.commit.assert_not_called()
        db.rollback.assert_called_once_with()

    def test_unique_identity_race_is_rejected_and_rolled_back(self) -> None:
        db = empty_database()
        db.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate username"))

        with self.assertRaisesRegex(BootstrapAdminError, "BOOTSTRAP_IDENTITY_CONFLICT"):
            create_bootstrap_admin(db, payload())

        db.commit.assert_not_called()
        db.rollback.assert_called_once_with()

    def test_transaction_failure_rolls_back(self) -> None:
        db = empty_database()
        db.commit.side_effect = RuntimeError("database unavailable")

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            create_bootstrap_admin(db, payload())

        db.rollback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
