"""HTTP middleware shared by every API route."""

from __future__ import annotations

import logging
from time import perf_counter

from fastapi import Request, Response

from app.core.exceptions import unknown_exception_handler
from app.core.request_context import (
    REQUEST_ID_HEADER,
    reset_request_id,
    resolve_request_id,
    set_request_id,
)


request_logger = logging.getLogger("app.request")


async def request_context_middleware(request: Request, call_next) -> Response:
    request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
    request.state.request_id = request_id
    token = set_request_id(request_id)
    started_at = perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            request_logger.error(
                "HTTP request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                    "exception_type": type(exc).__name__,
                },
            )
            response = await unknown_exception_handler(request, exc)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        response.headers[REQUEST_ID_HEADER] = request_id
        request_logger.info(
            "HTTP request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            },
        )
        return response
    finally:
        reset_request_id(token)
