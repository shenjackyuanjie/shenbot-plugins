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
        self.warnings: list[str] = []

    def send_message(self, message: str) -> bool:
        self.sent.append(message)
        return True

    def warn(self, text: str) -> None:
        self.warnings.append(text)


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
            config = SimpleNamespace(get_value=lambda key: "")
            with patch.object(
                score.PLUGIN_MANIFEST, "config_unchecked", return_value=config
            ):
                score.on_load()
            finish_query.set()
            deadline = time.monotonic() + 2.0
            while score._query_lock.locked() and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(score._query_lock.locked())
        self.assertEqual(
            client.sent,
            ["正在查询成绩；如 Edge 出现安全验证，请手动完成。"],
        )


class ScoreReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = patch.object(score, "_query_lock", threading.Lock())
        self.lock.start()
        self.addCleanup(self.lock.stop)
        score._shutdown.clear()
        self.client = FakeClient()
        self.message = FakeMessage()

    def test_rapid_commands_reserve_only_one_background_query(self) -> None:
        with (
            patch.object(score.threading, "Thread") as thread,
            patch.object(score, "query_scores", return_value="查询结果") as query,
        ):
            score.on_ica_message(self.message, self.client)
            score.on_ica_message(self.message, self.client)
            thread.assert_called_once()
            self.assertIn("请稍候", self.client.sent[-1])
            self.assertTrue(score._query_lock.locked())
            thread.call_args.kwargs["target"]()
            query.assert_called_once()
        self.assertFalse(score._query_lock.locked())
        self.assertEqual(self.client.sent[-1], "查询结果")

    def test_query_failure_replies_and_releases_slot(self) -> None:
        with patch.object(score.threading, "Thread") as thread:
            score.on_ica_message(self.message, self.client)
            with patch.object(
                score, "query_scores", side_effect=RuntimeError("fixture-private-token")
            ):
                thread.call_args.kwargs["target"]()
        self.assertFalse(score._query_lock.locked())
        self.assertIn("成绩查询失败", self.client.sent[-1])
        self.assertNotIn(
            "fixture-private-token", str(self.client.sent + self.client.warnings)
        )


if __name__ == "__main__":
    unittest.main()
