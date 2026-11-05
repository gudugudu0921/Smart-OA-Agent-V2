"""Password hashing and JWT access-token helpers."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

from app.core.config import settings


class AccessTokenError(ValueError):
    """Base exception for access-token validation failures."""


class AccessTokenExpiredError(AccessTokenError):
    """Raised when an access token has expired."""


class InvalidAccessTokenError(AccessTokenError):
    """Raised when an access token is malformed or has an invalid signature."""


class MissingTokenSubjectError(AccessTokenError):
    """Raised when an access token does not contain a usable subject."""


class MissingTokenIssuedAtError(AccessTokenError):
    """Raised when a legacy or malformed token has no issued-at timestamp."""


class PasswordPolicyError(ValueError):
    """Raised when a proposed password does not meet the minimum policy."""


def generate_temporary_password() -> str:
    """Return a cryptographically secure password intended for one-time display."""
    return secrets.token_urlsafe(16)


def utc_now_naive() -> datetime:
    """Return current UTC in the naive form used by MySQL DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_utc(value: datetime) -> datetime:
    """Normalize a database or timezone-aware datetime to aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_password_strength(password: str) -> None:
    """Enforce a small, predictable password policy compatible with bcrypt."""
    if len(password) < 8:
        raise PasswordPolicyError("PASSWORD_TOO_SHORT")
    if len(password) > 128 or len(password.encode("utf-8")) > 72:
        raise PasswordPolicyError("PASSWORD_TOO_LONG")

    categories = sum(
        (
            any(character.isalpha() for character in password),
            any(character.isdigit() for character in password),
            any(not character.isalnum() for character in password),
        )
    )
    if categories < 2:
        raise PasswordPolicyError("PASSWORD_TOO_WEAK")
    if password.casefold() in {
        "12345678",
        "password1",
        "qwerty123",
        "admin123",
    }:
        raise PasswordPolicyError("PASSWORD_TOO_WEAK")


def hash_password(password: str) -> str:
    """Return a salted bcrypt hash for a plaintext password."""
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > 72:
        raise ValueError("Password must not exceed 72 UTF-8 bytes for bcrypt")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Safely compare a plaintext password with a bcrypt hash."""
    try:
        password_bytes = plain_password.encode("utf-8")
        if len(password_bytes) > 72:
            return False
        return bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def _jwt_secret_key() -> str:
    secret = settings.jwt_secret_key.get_secret_value()
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY is not configured")
    return secret


def create_access_token(
    subject: str | int,
    expires_delta: timedelta | None = None,
    issued_after: datetime | None = None,
) -> str:
    """Create a signed JWT containing the user identifier as its subject."""
    issued_at = datetime.now(timezone.utc)
    if issued_after is not None:
        threshold = normalize_utc(issued_after)
        if issued_at <= threshold:
            issued_at = threshold + timedelta(microseconds=1)
    expires_at = issued_at + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    payload = {
        "sub": str(subject),
        # Keep microseconds so password changes and token issue times remain
        # strictly ordered even when both operations happen in one second.
        "iat": issued_at.timestamp(),
        "exp": expires_at,
    }
    return jwt.encode(
        payload,
        _jwt_secret_key(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict:
    """Decode and validate an access token, raising a specific token error."""
    try:
        payload = jwt.decode(
            token,
            _jwt_secret_key(),
            algorithms=[settings.jwt_algorithm],
        )
    except ExpiredSignatureError as exc:
        raise AccessTokenExpiredError("Access token has expired") from exc
    except JWTError as exc:
        raise InvalidAccessTokenError("Access token is invalid") from exc

    subject = payload.get("sub")
    if subject is None or not str(subject).strip():
        raise MissingTokenSubjectError("Access token subject is missing")
    issued_at = payload.get("iat")
    if not isinstance(issued_at, (int, float)):
        raise MissingTokenIssuedAtError("Access token issued-at timestamp is missing")
    return payload


def token_issued_at(payload: dict) -> datetime:
    issued_at = payload.get("iat")
    if not isinstance(issued_at, (int, float)):
        raise MissingTokenIssuedAtError("Access token issued-at timestamp is missing")
    try:
        return datetime.fromtimestamp(issued_at, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidAccessTokenError(
            "Access token issued-at timestamp is invalid"
        ) from exc
