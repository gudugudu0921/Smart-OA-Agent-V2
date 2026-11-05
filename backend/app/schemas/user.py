"""Public user response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserRole(str, Enum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    HR = "hr"
    ADMIN = "admin"


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    employee_no: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=64)
    email: EmailStr | None = None
    role: UserRole
    department: str = Field(min_length=1, max_length=64)
    position: str | None = Field(default=None, max_length=64)
    manager_id: int | None = Field(default=None, ge=1)

    @field_validator("username", "employee_no", "name", "department", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("position", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class UserBase(BaseModel):
    employee_no: str
    username: str
    name: str
    email: str | None = None
    role: str
    department: str
    position: str | None = None
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None
    password_changed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserResponse(UserBase):
    id: int
    manager_id: int | None = None
    created_at: datetime
    updated_at: datetime


class UserCreateResponse(BaseModel):
    user: UserResponse
    temporary_password: str


class UserStatusUpdate(BaseModel):
    is_active: bool


class UserPasswordResetResponse(BaseModel):
    user_id: int
    username: str
    temporary_password: str


class UserSummary(BaseModel):
    id: int
    employee_no: str
    name: str
    role: str
    department: str
    position: str | None = None

    model_config = ConfigDict(from_attributes=True)
