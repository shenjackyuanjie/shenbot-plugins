from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from plugins import score


class FakeMessage:
    is_from_self = False
    content = score.COMMAND
    sender_id = 1

    def reply_with(self, content: str) -> str:
        return content


class FakeClient:
    def __init__(self) -> None:
        self.status = SimpleNamespace(admins=[1])
        self.sent: list[str] = []

    def send_message(self, message: str) -> bool:
        self.sent.append(message)
        return True


class ScoreLifecycleTests(unittest.TestCase):
    def test_unloaded_generation_does_not_send_background_result(self) -> None:
        query_started = threading.Event()
        finish_query = threading.Event()

        def query_scores() -> str:
            query_started.set()
            self.assertTrue(finish_query.wait(timeout=2.0))
            return "查询结果"

        score._shutdown.clear()
        message = FakeMessage()
        client = FakeClient()
        with patch.object(score, "query_scores", side_effect=query_scores):
            score.on_ica_message(message, client)
            self.assertTrue(query_started.wait(timeout=2.0))
            score.on_unload()
            finish_query.set()
            deadline = time.monotonic() + 2.0
            while score._query_lock.locked() and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(score._query_lock.locked())
        self.assertEqual(
            client.sent,
            ["正在查询成绩；如 Edge 出现安全验证，请手动完成。"],
        )


if __name__ == "__main__":
    unittest.main()
