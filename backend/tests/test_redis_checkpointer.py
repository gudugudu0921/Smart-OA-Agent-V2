from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from redis.exceptions import RedisError

from app.db.redis import Redis7CheckpointSaver, RedisUnavailableError


class RedisCheckpointSetupTests(unittest.TestCase):
    def test_setup_checks_the_injected_client(self) -> None:
        client = MagicMock()
        saver = Redis7CheckpointSaver(client, key_prefix="isolated-test")

        with patch(
            "app.db.redis.ping_redis",
            side_effect=AssertionError("global client must not be used"),
        ):
            saver.setup()

        client.ping.assert_called_once_with()

    def test_setup_preserves_safe_unavailable_error_mapping(self) -> None:
        client = MagicMock()
        client.ping.side_effect = RedisError("redis://private-host:6379")
        saver = Redis7CheckpointSaver(client, key_prefix="isolated-test")

        with self.assertRaises(RedisUnavailableError) as raised:
            saver.setup()

        self.assertNotIn("private-host", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, RedisError)


if __name__ == "__main__":
    unittest.main()
