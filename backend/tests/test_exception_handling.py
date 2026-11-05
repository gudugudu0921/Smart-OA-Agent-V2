from __future__ import annotations

import io
import json
import logging
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.exceptions import AppException, install_exception_handlers
from app.core.logging import JsonLogFormatter
from app.core.middleware import request_context_middleware


class PasswordPayload(BaseModel):
    new_password: SecretStr
    count: int


def build_app() -> FastAPI:
    test_app = FastAPI()
    install_exception_handlers(test_app)
    test_app.middleware("http")(request_context_middleware)
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://frontend.test"],
        expose_headers=["X-Request-ID"],
    )

    @test_app.get("/app-error")
    def app_error() -> None:
        raise AppException("TEST_ERROR", "可安全展示的错误。", 409)

    @test_app.get("/http-error")
    def http_error() -> None:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    @test_app.post("/validate")
    def validate(payload: PasswordPayload) -> dict[str, bool]:
        return {"success": True}

    @test_app.get("/unknown")
    def unknown() -> None:
        raise RuntimeError("password=secret database-path=E:/private SQL SELECT *")

    @test_app.get("/database")
    def database() -> None:
        raise OperationalError(
            "SELECT password_hash FROM users",
            {"password": "database-secret"},
            RuntimeError("mysql://root:secret@127.0.0.1"),
        )

    @test_app.get("/integrity")
    def integrity() -> None:
        raise IntegrityError(
            "INSERT INTO users(password_hash)",
            {"password_hash": "secret"},
            RuntimeError("duplicate"),
        )

    return test_app


class ExceptionHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(build_app(), raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.client.close()

    def assert_error(self, response, status_code: int, code: str) -> dict:
        self.assertEqual(response.status_code, status_code)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], code)
        self.assertTrue(body["message"])
        self.assertIn("detail", body)
        self.assertEqual(body["request_id"], response.headers["x-request-id"])
        return body

    def test_app_exception_has_unified_shape(self) -> None:
        self.assert_error(self.client.get("/app-error"), 409, "TEST_ERROR")

    def test_http_exception_has_unified_shape(self) -> None:
        self.assert_error(self.client.get("/http-error"), 403, "PERMISSION_DENIED")

    def test_validation_error_omits_password_input(self) -> None:
        response = self.client.post(
            "/validate",
            json={"new_password": "Never-Echo-This", "count": "not-an-int"},
        )
        body = self.assert_error(response, 422, "VALIDATION_ERROR")
        serialized = str(body)
        self.assertNotIn("Never-Echo-This", serialized)
        self.assertNotIn("input", serialized)

    def test_unknown_route_is_unified(self) -> None:
        self.assert_error(self.client.get("/missing-route"), 404, "NOT_FOUND")

    def test_unknown_exception_is_safe(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLogFormatter())
        exception_logger = logging.getLogger("app.exceptions")
        exception_logger.addHandler(handler)
        try:
            response = self.client.get(
                "/unknown",
                headers={
                    "X-Request-ID": "unknown-error-request",
                    "Origin": "http://frontend.test",
                },
            )
        finally:
            exception_logger.removeHandler(handler)
        body = self.assert_error(response, 500, "INTERNAL_SERVER_ERROR")
        serialized = str(body)
        for secret in ("secret", "E:/private", "SELECT", "traceback"):
            self.assertNotIn(secret, serialized)
        log_entry = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(log_entry["request_id"], "unknown-error-request")
        self.assertIn("safe_traceback", log_entry)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://frontend.test",
        )
        for secret in ("password=secret", "E:/private", "SELECT *"):
            self.assertNotIn(secret, stream.getvalue())

    def test_database_connection_error_is_safe_503(self) -> None:
        body = self.assert_error(
            self.client.get("/database"),
            503,
            "DATABASE_UNAVAILABLE",
        )
        serialized = str(body)
        for secret in ("password_hash", "database-secret", "mysql://", "SELECT"):
            self.assertNotIn(secret, serialized)

    def test_integrity_error_is_conflict_not_outage(self) -> None:
        response = self.client.get("/integrity")
        body = self.assert_error(response, 409, "DATA_CONFLICT")
        self.assertNotIn("password_hash", str(body))


if __name__ == "__main__":
    unittest.main()
