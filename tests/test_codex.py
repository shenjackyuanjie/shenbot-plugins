"""账号隔离、群权限、真实摘要和查询生命周期的核心回归。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from plugins import codex

NOW = 1_800_000_000


class LoginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = temp.name
        self.path = Path(temp.name, ".credentials.yaml")
        self.payload = {
            "version": 1,
            "activeId": "second",
            "accounts": [
                {
                    "id": name,
                    "credential": {
                        "type": "oauth",
                        "access": "fixture-access-" + name,
                        "accountId": "fixture-account-" + name,
                        "expires": (NOW + 3600) * 1000,
                    },
                }
                for name in ("first", "second")
            ],
        }
        clock = patch.object(codex.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def save(self):
        self.path.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "records": {
                        "codex-subscription/accounts": {
                            "kind": "grant",
                            "payload": self.payload,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_current_account_is_reread_and_never_silently_replaced(self):
        self.save()
        original = self.path.read_bytes()
        self.assertEqual(
            codex.read_login(self.home).account_id, "fixture-account-second"
        )
        self.assertEqual(self.path.read_bytes(), original)
        self.payload["activeId"] = "first"
        self.payload["accounts"][0]["credential"]["access"] = "fixture-rotated"
        self.save()
        self.assertEqual(codex.read_login(self.home).access, "fixture-rotated")
        self.payload["activeId"] = "removed"
        self.save()
        with self.assertRaises(codex.QueryError):
            codex.read_login(self.home)

    def test_expired_login_stops_before_network_without_writing_credentials(self):
        self.payload["accounts"][1]["credential"]["expires"] = NOW * 1000
        self.save()
        original = self.path.read_bytes()
        with patch.object(codex, "fetch_usage") as fetch:
            with self.assertRaisesRegex(codex.QueryError, "已过期.*DSH"):
                codex.query_subscription(codex.Settings(dsh_home=self.home))
            fetch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)


class QueryTests(unittest.TestCase):
    def test_summary_identifies_weekly_quota_and_displays_reset_countdown(self):
        now = datetime.fromisoformat("2026-10-24T01:05:37+08:00").timestamp()
        reset = datetime.fromisoformat("2026-10-30T01:10:39+08:00").timestamp()
        text = codex.format_usage(
            {
                "plan_type": "pro",
                "email": "fixture@example.invalid",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 80,
                        "limit_window_seconds": 604800,
                        "reset_at": reset,
                    }
                },
            },
            now=now,
        )
        self.assertIn("套餐：Pro", text)
        self.assertIn("每周额度：剩余 20%", text)
        self.assertIn("重置：2026-10-30 01:10:39（还剩 6 天 0 小时 5 分钟）", text)
        self.assertIn("长周期额度：未返回", text)
        self.assertNotIn("fixture", text)

    def test_rejected_login_gives_dsh_help_not_private_upstream_error(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            codex.USAGE_URL, 401, "fixture-private-token", {}, None
        )
        with patch.object(codex.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(codex.QueryError) as error:
                codex.fetch_usage(codex.Login("fixture-access", "fixture-account"), 5)
        self.assertIn("DSH", str(error.exception))
        self.assertIn("登录已失效", str(error.exception))
        self.assertNotIn("fixture", str(error.exception))

    def test_server_rate_limit_is_preserved_for_retry_scheduling(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            codex.USAGE_URL, 429, "limited", {"Retry-After": "120"}, None
        )
        with patch.object(codex.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(codex.QueryError) as error:
                codex.fetch_usage(codex.Login("fixture-access", "fixture-account"), 5)
        self.assertEqual(error.exception.retry_after, 120)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.state = codex.Runtime(codex.Settings(frozenset({123456})))
        self.msg = SimpleNamespace(
            is_from_self=False,
            room_id=-123456,
            content="/codex check",
            reply_with=lambda text: text,
        )
        self.client = Mock()
        self.client.send_message.return_value = True
        runtime = patch.object(codex, "_runtime", self.state)
        runtime.start()
        self.addCleanup(runtime.stop)
        thread = patch.object(codex.threading, "Thread")
        self.thread = thread.start()
        self.addCleanup(thread.stop)

    def finish(self):
        self.thread.call_args.kwargs["target"]()

    def test_only_exact_check_in_whitelisted_group_can_query(self):
        for room in (-654321, 123456):
            codex.on_ica_message(
                SimpleNamespace(**(vars(self.msg) | {"room_id": room})), self.client
            )
        self.thread.assert_not_called()
        self.client.send_message.assert_not_called()
        # image 走独立的分钟级 worker，不能占用额度查询槽位。
        self.msg.content = "/codex image 一只猫"
        codex.on_ica_message(self.msg, self.client)
        self.assertEqual(self.thread.call_args.kwargs["name"], "codex-image")
        self.thread.reset_mock()
        self.msg.content = "/codex check"
        codex.on_ica_message(self.msg, self.client)
        self.assertEqual(self.thread.call_args.kwargs["name"], "codex-query")

    def test_image_subcommands_work_without_the_image_prefix(self):
        for command in (
            "/codex confirm",
            "/codex cancel",
            "/codex status",
            "/codex set --quality high",
            "/codex direct 一只猫",
            "/codex attach",
        ):
            self.thread.reset_mock()
            self.msg.content = command
            codex.on_ica_message(self.msg, self.client)
            self.assertEqual(
                self.thread.call_args.kwargs["name"], "codex-image", command
            )
            self.assertEqual(
                self.thread.call_args.kwargs["args"][1], command.split()[1], command
            )

    def test_help_is_local_during_query_and_respects_group_boundary(self):
        self.msg.content = "/codex help"
        self.msg.room_id = -654321
        codex.on_ica_message(self.msg, self.client)
        self.client.send_message.assert_not_called()
        self.msg.room_id = -123456
        self.state.busy = True
        self.state.next_query_at = float("inf")
        for command in ("/codex", "/codex help"):
            self.msg.content = command
            codex.on_ica_message(self.msg, self.client)
        self.assertEqual(self.client.send_message.call_count, 2)
        self.thread.assert_not_called()
        # 忙时帮助仍在本地立即返回，不占用查询槽位。
        self.assertTrue(self.state.busy)

    def test_single_query_reservation_is_released_after_failure(self):
        codex.on_ica_message(self.msg, self.client)
        codex.on_ica_message(self.msg, self.client)
        self.thread.assert_called_once()
        with patch.object(
            codex, "query_subscription", side_effect=codex.QueryError("查询失败")
        ):
            self.finish()
        self.assertFalse(self.state.busy)
        self.state.next_query_at = 0
        codex.on_ica_message(self.msg, self.client)
        with patch.object(codex, "query_subscription", return_value="已恢复"):
            self.finish()
        self.assertEqual(self.client.send_message.call_args.args[0], "已恢复")

    def test_old_generation_cannot_reply_after_unload_and_reload(self):
        codex.on_ica_message(self.msg, self.client)
        codex.on_unload()
        codex._runtime = codex.Runtime(self.state.settings)
        with patch.object(codex, "query_subscription", return_value="旧代结果"):
            self.finish()
        self.assertEqual(self.client.send_message.call_count, 1)
        self.assertFalse(self.state.busy)


class GenerationSlotTests(unittest.TestCase):
    def test_queue_rejects_when_waiting_is_full_and_wakes_on_release(self):
        slots = codex.GenerationSlots(concurrency=2, max_waiting=1)
        self.assertTrue(slots.acquire())
        self.assertTrue(slots.acquire())
        waiter = threading.Thread(target=slots.acquire, daemon=True)
        waiter.start()
        deadline = time.monotonic() + 2
        while slots.snapshot()[1] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(slots.snapshot(), (2, 1))
        # 并发已满且排队已满：新的确认直接拒绝，不占坑也不排队。
        self.assertFalse(slots.acquire())
        slots.release()
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(slots.snapshot(), (2, 0))


if __name__ == "__main__":
    unittest.main()
