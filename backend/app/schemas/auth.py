"""Authentication request and response schemas."""

from __future__ import annotations

from pydantic import BaseModel, SecretStr


class LoginRequest(BaseModel):
    username: str
    password: SecretStr


class ChangePasswordRequest(BaseModel):
    old_password: SecretStr
    new_password: SecretStr


class PasswordChangeResponse(BaseModel):
    message: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    sub: str
    iat: int | float
    exp: int | None = None
