from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from jose import jwt

from app.core.config import settings
from app.core.dependencies import get_current_business_user, get_current_user
from app.core.security import create_access_token, decode_access_token, hash_password
from app.models.user import User
from app.services.auth_service import change_password
from app.services.user_service import reset_user_password


def account(*, must_change: bool = False, changed_at: datetime | None = None) -> User:
    return User(
        id=2,
        username="employee_2",
        employee_no="EMP002",
        password_hash=hash_password("Old-Password-123"),
        name="Token Test",
        role="employee",
        department="Testing",
        is_active=True,
        must_change_password=must_change,
        password_changed_at=changed_at,
    )


class TokenPasswordInvalidationTests(unittest.TestCase):
    def test_token_created_after_password_change_is_accepted(self) -> None:
        changed_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=5)
        token = create_access_token(2, issued_after=changed_at)
        payload = decode_access_token(token)
        self.assertGreater(payload["iat"], int(changed_at.replace(tzinfo=timezone.utc).timestamp()))

        target = account(changed_at=changed_at)
        with patch("app.core.dependencies.get_user_by_id", return_value=target):
            self.assertIs(get_current_user(token=token, db=MagicMock()), target)

    def test_self_password_change_revokes_old_token_and_new_login_works(self) -> None:
        target = account()
        old_token = create_access_token(target.id)
        with patch("app.core.dependencies.get_user_by_id", return_value=target):
            self.assertIs(get_current_user(token=old_token, db=MagicMock()), target)

        changed_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
        with patch(
            "app.services.auth_service.utc_now_naive",
            return_value=changed_at,
        ):
            change_password(
                MagicMock(),
                user=target,
                old_password="Old-Password-123",
                new_password="New-Password-456!",
            )
        with patch("app.core.dependencies.get_user_by_id", return_value=target):
            with self.assertRaises(HTTPException):
                get_current_user(token=old_token, db=MagicMock())
            new_token = create_access_token(target.id, issued_after=target.password_changed_at)
            self.assertIs(get_current_user(token=new_token, db=MagicMock()), target)

    def test_admin_reset_revokes_users_old_token(self) -> None:
        target = account()
        old_token = create_access_token(target.id)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = target
        changed_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
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
            reset_user_password(db, user_id=target.id, actor_user_id=1)
        with patch("app.core.dependencies.get_user_by_id", return_value=target):
            with self.assertRaises(HTTPException):
                get_current_user(token=old_token, db=MagicMock())

    def test_token_from_same_second_as_password_change_is_revoked(self) -> None:
        changed_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        issued_at = int(changed_at.replace(tzinfo=timezone.utc).timestamp())
        token = jwt.encode(
            {
                "sub": "2",
                "iat": issued_at,
                "exp": issued_at + 3600,
            },
            settings.jwt_secret_key.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )
        with patch(
            "app.core.dependencies.get_user_by_id",
            return_value=account(changed_at=changed_at),
        ):
            with self.assertRaises(HTTPException) as raised:
                get_current_user(token=token, db=MagicMock())
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "TOKEN_REVOKED_BY_PASSWORD_CHANGE")

    def test_legacy_token_without_iat_returns_401(self) -> None:
        token = jwt.encode(
            {
                "sub": "2",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            settings.jwt_secret_key.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )
        with self.assertRaises(HTTPException) as raised:
            get_current_user(token=token, db=MagicMock())
        self.assertEqual(raised.exception.status_code, 401)

    def test_forced_password_user_is_blocked_from_business_dependency(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            get_current_business_user(account(must_change=True))
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail["code"], "PASSWORD_CHANGE_REQUIRED")

    def test_changed_password_user_passes_business_dependency(self) -> None:
        target = account(must_change=False)
        self.assertIs(get_current_business_user(target), target)


if __name__ == "__main__":
    unittest.main()
