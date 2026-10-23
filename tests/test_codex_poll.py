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
        # 同周期小幅回升再跌回已报告的档位，也不能重复提醒。
        self.fetch.return_value = usage(76)
        self.poll()
        self.fetch.return_value = usage(74)
        self.poll()
        self.watch.send_text.assert_called_once()

    def test_reset_and_account_switch_rebaseline_instead_of_reporting_false_drop(self):
        changes, baseline = codex.quota_changes(
            snapshot(96), snapshot(94, RESET + 604800)
        )
        self.assertEqual(changes, [])
        changes, _ = codex.quota_changes(baseline, snapshot(89, RESET + 604800))
        self.assertIn("90%", changes[0])
        changes, _ = codex.quota_changes(
            snapshot(96), snapshot(20, owner="fixture-other-owner")
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

    def test_unload_closes_active_watch_and_prevents_late_notification(self):
        self.state.poll_watch = self.watch
        self.state.baselines[123456] = snapshot(98)
        codex.on_unload()
        self.watch.close.assert_called_once()
        self.poll()
        self.watch.send_text.assert_not_called()
        self.assertFalse(self.state.busy)

    def test_scheduler_repeats_after_ten_minutes_and_stops_on_unload(self):
        with (
            patch.object(codex, "Scheduler") as scheduler,
            patch.object(codex, "_start_poll") as start,
        ):
            codex._schedule_poll(self.state, delay=1)
            callback = scheduler.call_args.args[0]
            callback()
            start.assert_called_once_with(self.state)
            self.assertEqual(scheduler.call_args.args[1].total_seconds(), 600)
            codex.on_unload()
            scheduler.reset_mock()
            callback()
            scheduler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
