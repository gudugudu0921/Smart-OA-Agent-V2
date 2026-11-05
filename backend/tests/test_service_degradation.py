from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.dependencies import get_current_user
from app.core.llm import LLMConfigurationError, LLMUnavailableError
from app.db.mysql import get_db
from app.db.redis import RedisUnavailableError
from app.main import app
from app.models.user import User


def employee() -> User:
    now = datetime.now()
    return User(
        id=2,
        username="phase1_employee",
        employee_no="PHASE1002",
        password_hash="not-returned",
        name="Phase 1 Test",
        role="employee",
        department="Testing",
        is_active=True,
        must_change_password=False,
        created_at=now,
        updated_at=now,
    )


class HealthAndReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.client.close()

    def test_all_dependencies_healthy(self) -> None:
        with (
            patch("app.services.health_service.check_database", return_value=True),
            patch("app.services.health_service.check_redis", return_value=True),
        ):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "application": "ok",
                "database": "ok",
                "redis": "ok",
            },
        )
        self.assertIn("x-request-id", response.headers)

    def test_redis_failure_degrades_health_but_not_readiness(self) -> None:
        with (
            patch("app.services.health_service.check_database", return_value=True),
            patch("app.services.health_service.check_redis", return_value=False),
        ):
            health = self.client.get("/health")
            ready = self.client.get("/ready")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "degraded")
        self.assertEqual(health.json()["redis"], "error")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), {"status": "ready", "database": "ok"})

    def test_database_failure_degrades_health_and_blocks_readiness(self) -> None:
        with (
            patch("app.services.health_service.check_database", return_value=False),
            patch("app.services.health_service.check_redis", return_value=True),
        ):
            health = self.client.get("/health")
            ready = self.client.get("/ready")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["database"], "error")
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(
            ready.json(),
            {"status": "not_ready", "database": "error"},
        )
        self.assertIn("x-request-id", ready.headers)

    def test_legacy_redis_health_uses_safe_unified_error(self) -> None:
        with patch(
            "app.main.ping_redis",
            side_effect=RedisUnavailableError("redis://secret-host:6379"),
        ):
            response = self.client.get("/health/redis")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "REDIS_UNAVAILABLE")
        self.assertNotIn("secret-host", str(response.json()))


class OptionalDependencyBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[get_current_user] = employee
        app.dependency_overrides[get_db] = lambda: object()
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.client.close()

    def assert_leave_still_works(self) -> None:
        with patch(
            "app.api.routes.leave.get_my_leave_applications",
            return_value=[],
        ):
            response = self.client.get("/leave/my")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_redis_failure_only_blocks_chat(self) -> None:
        with patch(
            "app.api.routes.chat.create_session",
            side_effect=RedisUnavailableError("redis://private-host"),
        ):
            response = self.client.post("/chat/sessions")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "REDIS_UNAVAILABLE")
        self.assertNotIn("private-host", str(response.json()))
        self.assert_leave_still_works()

    def _chat_failure(self, failure: Exception, expected_code: str) -> None:
        turn_lock = MagicMock()
        turn_lock.__enter__.return_value = turn_lock
        turn_lock.__exit__.return_value = None
        with (
            patch("app.api.routes.chat.validate_session_owner"),
            patch(
                "app.api.routes.chat.acquire_chat_turn_lock",
                return_value=turn_lock,
            ),
            patch("app.api.routes.chat.run_chat_agent", side_effect=failure),
        ):
            response = self.client.post(
                "/chat",
                json={"thread_id": str(uuid4()), "message": "测试"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], expected_code)
        self.assertNotIn("secret", str(response.json()))
        self.assert_leave_still_works()

    def test_llm_not_configured_only_blocks_chat(self) -> None:
        self._chat_failure(
            LLMConfigurationError("api_key=secret endpoint=private"),
            "LLM_NOT_CONFIGURED",
        )

    def test_llm_runtime_failure_only_blocks_chat(self) -> None:
        self._chat_failure(
            LLMUnavailableError("provider secret payload"),
            "LLM_UNAVAILABLE",
        )

    def test_raw_llm_invocation_error_is_wrapped_safely(self) -> None:
        from app.agents.recognition import recognize_intent

        state = {
            "messages": [],
            "user_message": "帮我请假",
            "user_role": "employee",
            "pending_intent": None,
            "pending_fields": [],
            "extracted_info": {},
            "last_query_results": [],
            "application_id": None,
            "turn_count": 0,
        }
        with patch(
            "app.agents.recognition.get_llm",
            side_effect=RuntimeError("provider api_key=secret"),
        ):
            with self.assertRaises(LLMUnavailableError):
                recognize_intent(state)


if __name__ == "__main__":
    unittest.main()
