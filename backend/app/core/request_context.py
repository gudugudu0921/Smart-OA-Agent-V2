"""Request-scoped identifiers and safe client metadata helpers."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from uuid import uuid4

from fastapi import Request


REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_LENGTH = 64
MAX_USER_AGENT_LENGTH = 256
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def resolve_request_id(client_value: str | None) -> str:
    """Reuse a safe client ID or return a new UUID string."""
    if client_value and _SAFE_REQUEST_ID.fullmatch(client_value):
        return client_value
    return str(uuid4())


def set_request_id(request_id: str) -> Token[str | None]:
    return _request_id.set(request_id)


def get_request_id() -> str | None:
    return _request_id.get()


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def get_client_ip(request: Request) -> str | None:
    """Return only the direct peer IP; trusted proxy handling is deferred."""
    return request.client.host if request.client is not None else None


def get_safe_user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return value[:MAX_USER_AGENT_LENGTH] if value else None
