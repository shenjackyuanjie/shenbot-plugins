from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_thread_start_failure_releases_slot_for_retry(self) -> None:
        with patch.object(score.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("启动失败")
            client = Mock(status=SimpleNamespace(admins=[1]))
            score.on_ica_message(self.message, client)
            self.assertFalse(score._query_lock.locked())
            self.assertIn("启动成绩查询失败", client.send_message.call_args.args[0])
            thread.return_value.start.side_effect = None
            score.on_ica_message(self.message, client)
            self.assertTrue(score._query_lock.locked())
            with patch.object(score, "query_scores", return_value="重试结果"):
                thread.call_args.kwargs["target"]()
        self.assertFalse(score._query_lock.locked())

    def test_initial_reply_failure_releases_slot_without_starting_thread(self) -> None:
        client = Mock(status=SimpleNamespace(admins=[1]))
        client.send_message.side_effect = [RuntimeError("发送失败"), True]
        with patch.object(score.threading, "Thread") as thread:
            score.on_ica_message(self.message, client)
        thread.assert_not_called()
        self.assertFalse(score._query_lock.locked())

    def test_query_failure_replies_and_releases_slot(self) -> None:
        with patch.object(score.threading, "Thread") as thread:
            score.on_ica_message(self.message, self.client)
            with patch.object(
                score, "query_scores", side_effect=RuntimeError("模拟错误")
            ):
                thread.call_args.kwargs["target"]()
        self.assertFalse(score._query_lock.locked())
        self.assertEqual(self.client.sent[-1], "成绩查询失败：模拟错误")

    def test_result_send_failure_also_releases_slot(self) -> None:
        client = Mock(status=SimpleNamespace(admins=[1]))
        with patch.object(score.threading, "Thread") as thread:
            score.on_ica_message(self.message, client)
            client.send_message.side_effect = RuntimeError("发送失败")
            with patch.object(score, "query_scores", return_value="查询结果"):
                with self.assertRaises(RuntimeError):
                    thread.call_args.kwargs["target"]()
        self.assertFalse(score._query_lock.locked())


if __name__ == "__main__":
    unittest.main()
