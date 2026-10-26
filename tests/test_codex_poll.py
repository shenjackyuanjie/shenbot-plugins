"""额度提醒的关键规则：跨档去重、换周期、失败恢复和旧代停止。"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plugins import codex

RESET = 1_900_000_000


def usage(remaining, reset=RESET):
    return {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": 100 - remaining,
                "limit_window_seconds": 604800,
                "reset_at": reset,
            }
        },
    }


def snapshot(remaining, reset=RESET, owner="fixture-owner"):
    return codex.quota_snapshot(owner, usage(remaining, reset))


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.state = codex.Runtime(codex.Settings(frozenset({123456})))
        self.connection = SimpleNamespace(ready=True, generation=7, account=1)
        self.watch = Mock()
        self.watch.send_text.return_value = True
        runtime = patch.object(codex, "_runtime", self.state)
        runtime.start()
        self.addCleanup(runtime.stop)
        login = patch.object(
            codex,
            "read_login",
            return_value=codex.Login("fixture-access", "fixture-owner"),
        )
        login.start()
        self.addCleanup(login.stop)
        fetch = patch.object(codex, "fetch_usage", return_value=usage(83))
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)

    def poll(self):
        self.state.poll_watch = self.watch
        self.state.busy = True
        codex._poll_worker(self.state, self.watch, self.connection)

    def test_first_poll_is_silent_then_crossed_levels_are_combined_and_not_repeated(
        self,
    ):
        self.poll()
        self.watch.send_text.assert_not_called()
        self.fetch.return_value = usage(74)
        self.poll()
        self.watch.send_text.assert_called_once()
        generation, account, room, text, _ = self.watch.send_text.call_args.args
        self.assertEqual((generation, account, room), (7, 1, -123456))
        self.assertIn("80% / 75%", text)
        self.assertIn("套餐：Pro", text)
        # 任何上涨都报 reset；重复的上涨后余额和已报告的下降档位不再提醒。
        self.fetch.return_value = usage(76)
        self.poll()
        text = self.watch.send_text.call_args.args[3]
        self.assertIn("reset", text)
        self.assertIn("74% → 76%", text)
        self.assertIn("浪费了74%额度😭", text)
        self.poll()
        self.fetch.return_value = usage(74)
        self.poll()
        self.assertEqual(self.watch.send_text.call_count, 2)

    def test_reset_reports_last_balance_but_zero_has_no_waste_warning(self):
        self.state.baselines[123456] = snapshot(74)
        self.fetch.return_value = usage(76)
        self.poll()
        self.fetch.return_value = usage(75)
        self.poll()
        self.assertEqual(self.watch.send_text.call_count, 1)
        self.fetch.return_value = usage(100, RESET + 604800)
        self.poll()
        text = self.watch.send_text.call_args.args[3]
        self.assertIn("75% → 100%", text)
        self.assertIn("浪费了75%额度😭", text)
        self.fetch.return_value = usage(0, RESET + 604800)
        self.poll()
        self.fetch.return_value = usage(100, RESET + 2 * 604800)
        self.poll()
        text = self.watch.send_text.call_args.args[3]
        self.assertIn("reset", text)
        self.assertIn("0% → 100%", text)
        self.assertNotIn("浪费", text)
        self.poll()
        self.assertEqual(self.watch.send_text.call_count, 4)

    def test_cycle_change_and_account_switch_do_not_invent_notifications(self):
        changes, baseline = codex.quota_changes(
            snapshot(96), snapshot(94, RESET + 604800)
        )
        self.assertEqual(changes, [])
        changes, _ = codex.quota_changes(baseline, snapshot(89, RESET + 604800))
        self.assertIn("90%", changes[0])
        changes, _ = codex.quota_changes(
            snapshot(20), snapshot(100, owner="fixture-other-owner")
        )
        self.assertEqual(changes, [])

    def test_failed_poll_keeps_baseline_and_recovers_without_losing_crossings(self):
        self.state.baselines[123456] = snapshot(98)
        self.fetch.side_effect = codex.QueryError("上游暂不可用", retry_after=120)
        with patch("builtins.print"):
            self.poll()
        self.assertFalse(self.state.busy)
        self.assertIsNone(self.state.poll_watch)
        self.assertEqual(self.state.baselines[123456].windows[604800].remaining, 98)
        self.assertGreater(self.state.next_query_at, time.monotonic() + 119)
        self.watch.send_text.assert_not_called()
        self.fetch.side_effect = None
        self.poll()
        self.assertIn("95% / 90% / 85%", self.watch.send_text.call_args.args[3])

    def test_multiple_groups_keep_independent_delivery_progress(self):
        self.state.settings = codex.Settings(frozenset({123456, 654321}))
        self.poll()
        self.watch.send_text.assert_not_called()
        self.fetch.return_value = usage(74)
        self.watch.send_text.side_effect = [False, True]
        with patch("builtins.print"):
            self.poll()
        self.assertEqual(
            [call.args[2] for call in self.watch.send_text.call_args_list],
            [-123456, -654321],
        )
        self.watch.send_text.reset_mock(side_effect=True)
        self.poll()
        self.watch.send_text.assert_called_once()
        self.assertEqual(self.watch.send_text.call_args.args[2], -123456)

    def test_alerts_follow_dedicated_alert_groups(self):
        # 提醒群与命令白名单分离：白话群里仍能手动查询，但自动提醒只发配置的群。
        self.state.settings = codex.Settings(
            frozenset({123456, 654321}), alert_groups=frozenset({654321})
        )
        self.poll()
        self.fetch.return_value = usage(74)
        self.poll()
        self.watch.send_text.assert_called_once()
        self.assertEqual(self.watch.send_text.call_args.args[2], -654321)

    def test_unload_closes_active_watch_and_prevents_late_notification(self):
        self.state.poll_watch = self.watch
        self.state.baselines[123456] = snapshot(98)
        codex.on_unload()
        self.watch.close.assert_called_once()
        self.poll()
        self.watch.send_text.assert_not_called()
        self.assertFalse(self.state.busy)

    def test_scheduler_repeats_after_two_minutes_and_stops_on_unload(self):
        with (
            patch.object(codex, "Scheduler") as scheduler,
            patch.object(codex, "_start_poll") as start,
        ):
            codex._schedule_poll(self.state, delay=1)
            callback = scheduler.call_args.args[0]
            callback()
            start.assert_called_once_with(self.state)
            self.assertEqual(scheduler.call_args.args[1].total_seconds(), 120)
            codex.on_unload()
            scheduler.reset_mock()
            callback()
            scheduler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
