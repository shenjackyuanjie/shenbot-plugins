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
        self.reserve("15:00")
        self.reserve("14:00")
        self.assertEqual(len(self.tasks), 2)
        self.now = sign.SIGN_PLAN[-1]
        self.tasks[0].callback()
        self.client.send_room_sign_in.assert_not_called()
        self.tasks[1].callback()
        self.now += timedelta(hours=1)
        self.tasks[0].callback()
        self.client.send_room_sign_in.assert_called_once_with(-1)

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

    def test_delayed_callback_does_not_sign_on_wrong_day(self) -> None:
        self.reserve("13:00")
        self.now = datetime(2026, 10, 24, 13)
        self.tasks[0].callback()
        self.client.send_room_sign_in.assert_not_called()
        self.assertNotIn(-1, sign.SIGN_PLAN)


if __name__ == "__main__":
    unittest.main()
