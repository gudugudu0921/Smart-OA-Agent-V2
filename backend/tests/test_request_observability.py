from __future__ import annotations

import asyncio
import io
import json
import logging
import unittest
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.logging import JsonLogFormatter
from app.core.middleware import request_context_middleware
from app.core.request_context import (
    get_request_id,
    reset_request_id,
    set_request_id,
)


def build_test_app() -> FastAPI:
    test_app = FastAPI()
    test_app.middleware("http")(request_context_middleware)

    @test_app.api_route("/context", methods=["GET", "POST"])
    async def context(request: Request) -> dict[str, str | None]:
        await request.body()
        return {
            "state": request.state.request_id,
            "context": get_request_id(),
        }

    return test_app


class RequestIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(build_test_app())

    def tearDown(self) -> None:
        self.client.close()

    def test_generated_id_reaches_state_context_and_response(self) -> None:
        response = self.client.get("/context")
        request_id = response.headers["x-request-id"]
        UUID(request_id)
        self.assertEqual(response.json(), {"state": request_id, "context": request_id})
        self.assertIsNone(get_request_id())

    def test_safe_client_id_is_reused(self) -> None:
        response = self.client.get(
            "/context",
            headers={"X-Request-ID": "client_Request-123"},
        )
        self.assertEqual(response.headers["x-request-id"], "client_Request-123")

    def test_invalid_or_oversized_client_id_is_replaced(self) -> None:
        for value in ("bad request id!", "x" * 65):
            with self.subTest(value=value[:20]):
                response = self.client.get("/context", headers={"X-Request-ID": value})
                generated = response.headers["x-request-id"]
                UUID(generated)
                self.assertNotEqual(generated, value)

    def test_request_log_excludes_headers_body_and_query(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLogFormatter())
        logger = logging.getLogger("app.request")
        logger.addHandler(handler)
        try:
            response = self.client.post(
                "/context?token=query-secret",
                headers={
                    "Authorization": "Bearer auth-secret",
                    "X-Request-ID": "safe-log-id",
                },
                json={"new_password": "body-secret"},
            )
        finally:
            logger.removeHandler(handler)
        self.assertEqual(response.status_code, 200)
        entries = [json.loads(line) for line in stream.getvalue().splitlines()]
        entry = entries[-1]
        self.assertEqual(entry["request_id"], "safe-log-id")
        self.assertEqual(entry["path"], "/context")
        output = stream.getvalue()
        for secret in ("query-secret", "auth-secret", "body-secret", "Authorization"):
            self.assertNotIn(secret, output)


class ContextIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_values_are_isolated_across_tasks_and_reset(self) -> None:
        async def worker(value: str) -> tuple[str | None, str | None]:
            token = set_request_id(value)
            try:
                await asyncio.sleep(0)
                during = get_request_id()
            finally:
                reset_request_id(token)
            return during, get_request_id()

        first, second = await asyncio.gather(worker("request-a"), worker("request-b"))
        self.assertEqual(first, ("request-a", None))
        self.assertEqual(second, ("request-b", None))
        self.assertIsNone(get_request_id())


if __name__ == "__main__":
    unittest.main()
