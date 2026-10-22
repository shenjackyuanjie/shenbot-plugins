from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plugins import sign


class SignReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 23, 12)
        self.clock = patch.object(sign, "datetime", wraps=datetime)
        self.fake_datetime = self.clock.start()
        self.fake_datetime.now.side_effect = lambda: self.now
        self.addCleanup(self.clock.stop)
        for name in ("SIGN_REC", "SIGN_PLAN", "_sign_tokens"):
            state = patch.object(sign, name, {})
            state.start()
            self.addCleanup(state.stop)
        self.time_state = patch.object(sign, "SIGN_TIME", self.now)
        self.time_state.start()
        self.addCleanup(self.time_state.stop)
        self.tasks = []

        def make_task(callback, delay):
            task = Mock()
            task.callback = callback
            task.delay = delay
            self.tasks.append(task)
            return task

        self.scheduler = patch.object(sign, "Scheduler", side_effect=make_task)
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)
        self.message = Mock(room_id=-1, sender_id=100)
        self.message.reply_with.side_effect = lambda text: text
        self.client = Mock()
        self.client.send_room_sign_in.return_value = True

    def reserve(self, when: str) -> None:
        self.message.content = f"/bot-sign want {when}"
        sign.handle_sign_want(self.message, self.client)

    def test_duplicate_today_reservation_schedules_only_once(self) -> None:
        self.reserve("13:00")
        self.reserve("13:00")
        self.assertEqual(len(self.tasks), 1)
        self.assertIn("无需重复", self.client.send_message.call_args.args[0])
        self.now = self.now.replace(hour=13)
        self.tasks[0].callback()
        self.tasks[0].callback()
        self.client.send_room_sign_in.assert_called_once_with(-1)
        self.assertNotIn(-1, sign.SIGN_PLAN)

    def test_earlier_reservation_invalidates_old_callback(self) -> None:
        for first, earlier in (("15:00", "14:00"), ("10:00", "09:00")):
            with self.subTest(first=first):
                sign.SIGN_REC.clear()
                sign.SIGN_PLAN.clear()
                sign._sign_tokens.clear()
                self.tasks.clear()
                self.client.reset_mock()
                self.now = datetime(2026, 10, 23, 12)
                self.reserve(first)
                self.reserve(earlier)
                self.assertEqual(len(self.tasks), 2)
                target = sign.SIGN_PLAN[-1]
                self.now = target
                self.tasks[0].callback()
                self.client.send_room_sign_in.assert_not_called()
                self.assertEqual(sign.SIGN_PLAN[-1], target)
                self.tasks[1].callback()
                self.now = target + timedelta(hours=1)
                self.tasks[0].callback()
                self.client.send_room_sign_in.assert_called_once_with(-1)

    def test_later_reservation_keeps_original_plan(self) -> None:
        self.reserve("13:00")
        self.reserve("14:00")
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(sign.SIGN_PLAN[-1].hour, 13)
        self.assertIn("保留原计划", self.client.send_message.call_args.args[0])

    def test_midnight_preserves_pending_plan_without_marking_signed(self) -> None:
        self.reserve("10:00")
        self.now = datetime(2026, 10, 24)
        sign.reset_daily_state(self.now)
        self.assertNotIn(-1, sign.SIGN_REC)
        self.assertIn(-1, sign.SIGN_PLAN)
        self.now = self.now.replace(hour=10)
        self.tasks[0].callback()
        self.client.send_room_sign_in.assert_called_once_with(-1)
        sign.reset_daily_state(self.now)
        self.assertIn(-1, sign.SIGN_REC)

    def test_batch_sign_before_callback_suppresses_duplicate(self) -> None:
        self.reserve("13:00")
        with patch.object(sign.time, "sleep"):
            sign.send_room_signs(self.client, [0], [SimpleNamespace(room_id=-1)])
        self.now = self.now.replace(hour=13)
        self.tasks[0].callback()
        self.client.send_room_sign_in.assert_called_once_with(-1)

    def test_rejected_sign_can_be_reserved_again(self) -> None:
        self.reserve("13:00")
        self.now = self.now.replace(hour=13)
        self.client.send_room_sign_in.return_value = False
        self.tasks[0].callback()
        self.assertNotIn(-1, sign.SIGN_REC)
        self.assertNotIn(-1, sign.SIGN_PLAN)
        self.reserve("14:00")
        self.assertEqual(len(self.tasks), 2)

    def test_failed_reschedule_restores_previous_callback(self) -> None:
        self.reserve("15:00")
        self.now = self.now.replace(hour=12, minute=1)
        with patch.object(sign, "Scheduler", side_effect=RuntimeError("调度不可用")):
            self.reserve("14:00")
        self.assertEqual(sign.SIGN_PLAN[-1].hour, 15)
        self.assertIn("失败", self.client.send_message.call_args.args[0])
        self.now = self.now.replace(hour=15)
        self.tasks[0].callback()
        self.client.send_room_sign_in.assert_called_once_with(-1)

    def test_scheduler_start_failure_clears_reservation_for_retry(self) -> None:
        failed_task = Mock()
        failed_task.start.side_effect = RuntimeError("启动失败")
        with patch.object(sign, "Scheduler", return_value=failed_task):
            self.reserve("13:00")
        self.assertNotIn(-1, sign.SIGN_PLAN)
        self.assertNotIn(-1, sign._sign_tokens)
        self.reserve("13:00")
        self.assertEqual(len(self.tasks), 1)

    def test_delayed_callback_does_not_sign_on_wrong_day(self) -> None:
        self.reserve("13:00")
        self.now = datetime(2026, 10, 24, 13)
        self.tasks[0].callback()
        self.client.send_room_sign_in.assert_not_called()
        self.assertNotIn(-1, sign.SIGN_PLAN)

    def test_invalid_reservation_returns_usage_without_scheduling(self) -> None:
        for content in (
            "/bot-sign want",
            "/bot-sign want 25:00",
            "/bot-sign want13:00",
            "/bot-sign want 13:00 extra",
        ):
            self.message.content = content
            sign.handle_sign_want(self.message, self.client)
            self.assertIn("用法", self.client.send_message.call_args.args[0])
        self.assertEqual(self.tasks, [])


if __name__ == "__main__":
    unittest.main()
