"""账号隔离、群权限、真实摘要和查询生命周期的核心回归。"""

from __future__ import annotations

import calendar
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch
from urllib.parse import parse_qs

import yaml

from plugins import codex
from plugins.codex_support import archive as archive_module
from plugins.codex_support import image_service

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
        with (
            patch.object(codex.urllib.request, "build_opener", return_value=opener),
            self.assertRaises(codex.QueryError) as error,
        ):
            codex.fetch_usage(codex.Login("fixture-access", "fixture-account"), 5)
        self.assertIn("DSH", str(error.exception))
        self.assertIn("登录已失效", str(error.exception))
        self.assertNotIn("fixture", str(error.exception))

    def test_server_rate_limit_is_preserved_for_retry_scheduling(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            codex.USAGE_URL, 429, "limited", {"Retry-After": "120"}, None
        )
        with (
            patch.object(codex.urllib.request, "build_opener", return_value=opener),
            self.assertRaises(codex.QueryError) as error,
        ):
            codex.fetch_usage(codex.Login("fixture-access", "fixture-account"), 5)
        self.assertEqual(error.exception.retry_after, 120)


class InlineKeyTests(unittest.TestCase):
    def load(self, **values):
        """按宿主配置形状加载一次插件，返回设置与本次日志。"""
        values.setdefault("allowed_groups", [123456])
        values.setdefault("timeout_seconds", 15)
        values.setdefault("cooldown_seconds", 15)
        values.setdefault("image_concurrency", 3)
        values.setdefault("image_queue_limit", 5)
        values.setdefault("alert_groups", [])
        config = Mock()
        config.get_value.side_effect = lambda name: values.get(name)
        with (
            patch.object(
                codex.PLUGIN_MANIFEST, "config_unchecked", return_value=config
            ),
            patch.object(codex, "_schedule_poll"),
            patch.object(codex, "_runtime", codex.Runtime(codex.Settings())),
            patch.object(codex, "warn") as warn,
            patch.object(codex, "info") as info,
        ):
            codex.on_load()
            settings = codex._runtime.settings
            emitted = [str(call) for call in warn.call_args_list + info.call_args_list]
        return settings, emitted

    def test_inline_keys_are_read_from_config_and_never_rendered(self):
        """防止 Key 退回文件路径，或出现在 repr 与日志里。"""
        settings, emitted = self.load(
            image_backend="openai",
            text_backend="openai",
            openai_text_api_key="  fixture-text  ",
            openai_image_api_key="fixture-image",
        )
        self.assertEqual(settings.openai_text_api_key, "fixture-text")
        self.assertEqual(settings.openai_image_api_key, "fixture-image")
        self.assertNotIn("fixture-text", repr(settings))
        self.assertNotIn("fixture-image", repr(settings))
        for line in emitted:
            self.assertNotIn("fixture-text", line)
            self.assertNotIn("fixture-image", line)

    def test_missing_inline_key_only_warns_so_check_stays_available(self):
        """缺 Key 不能挡住插件加载：check 与额度提醒要照常可用，告警指出字段名。"""
        for values, field in (
            (
                {"image_backend": "openai", "openai_text_api_key": "fixture-text"},
                "openai_image_api_key",
            ),
            (
                {"text_backend": "openai", "openai_image_api_key": "fixture-image"},
                "openai_text_api_key",
            ),
        ):
            with self.subTest(field=field):
                settings, emitted = self.load(**values)
                self.assertEqual(getattr(settings, field), "")
                self.assertTrue(any(field in line for line in emitted))
                for line in emitted:
                    self.assertNotIn("fixture-text", line)
                    self.assertNotIn("fixture-image", line)

    def test_subscription_defaults_load_without_any_openai_key(self):
        """默认订阅 + DeepSeek 不需要 OpenAI Key，加载不应报错或告警。"""
        settings, emitted = self.load()
        self.assertEqual(
            (settings.image_backend, settings.text_backend), ("codex", "deepseek")
        )
        self.assertEqual(settings.openai_text_api_key, "")
        self.assertEqual(settings.openai_image_api_key, "")
        self.assertEqual(emitted, [])


class UsageTests(unittest.TestCase):
    def setUp(self):
        codex._reset_usage_cache()
        self.addCleanup(codex._reset_usage_cache)

    def test_unconfigured_usage_key_sends_no_request(self):
        """未配置用量 Key 时 check 保持原输出，不发任何额外请求。"""
        settings = codex.Settings()
        with patch.object(codex, "fetch_openai_costs") as fetch:
            self.assertEqual(codex.openai_usage_section(settings), "")
            fetch.assert_not_called()

    def test_spend_and_monthly_limit_are_shown_as_estimates(self):
        """上限从分换算成美元，三天金额相加，剩余额度正确，且不回显用量 Key。"""
        settings = codex.Settings(openai_usage_api_key="fixture-usage")
        payload = {
            "data": [
                {
                    "start_time": NOW,
                    "results": [{"amount": {"value": 0.03, "currency": "usd"}}],
                },
                {
                    "start_time": NOW + 86400,
                    # 上游金额既可能是数字也可能是字符串，都要按金额解析。
                    "results": [{"amount": {"value": "0.05", "currency": "usd"}}],
                },
                {
                    "start_time": NOW + 2 * 86400,
                    "results": [{"amount": {"value": 0.04, "currency": "usd"}}],
                },
            ]
        }
        with (
            patch.object(codex, "fetch_openai_costs", return_value=payload),
            # threshold_amount 是分（10000 = $100），currency 是大写 USD。
            patch.object(
                codex,
                "fetch_openai_spend_limit",
                return_value={"threshold_amount": 10000, "currency": "USD"},
            ),
        ):
            text = codex.openai_usage_section(settings, now=NOW + 3 * 86400)
        # 只锁算出来的数，不锁措辞：10000 分换算成 $100.00，三天金额相加得 $0.12，
        # 剩余 $99.88，字符串金额也要计入。
        self.assertIn("$100.00", text)
        self.assertIn("$0.12", text)
        self.assertIn("$99.88", text)
        self.assertLessEqual(len(text.splitlines()), 5)
        self.assertNotIn("fixture-usage", text)

        # 非法金额不能被当成 0。
        with self.assertRaises(codex.UsageError):
            codex.format_openai_costs(
                {"data": [{"results": [{"amount": {"value": "not-a-number"}}]}]}
            )

    def test_cross_currency_limit_does_not_invent_remaining(self):
        """跨币种不能相减：只并列展示上限与已花费，同币种才给「剩余」。"""
        costs = {
            "data": [
                {
                    "start_time": NOW,
                    "results": [{"amount": {"value": "0.12", "currency": "eur"}}],
                }
            ]
        }
        mixed = codex.format_openai_costs(costs, spend_limit=(100.0, "USD"))
        self.assertIn("本月上限 $100.00", mixed)
        self.assertIn("已花费估算 EUR 0.12", mixed)
        self.assertNotIn("按上限剩余", mixed)

        same = codex.format_openai_costs(costs, spend_limit=(100.0, "EUR"))
        self.assertIn("按上限剩余（估算）EUR 99.88", same)

    def test_month_window_starts_at_utc_month_and_caps_buckets(self):
        """月初切换、闰年 2 月与分桶上限，决定查询窗口是否正确。"""
        for label, moment, expected, days in (
            ("月初当天", (2026, 12, 1, 0, 30), (2026, 12, 1), 1),
            ("跨月最后一天", (2026, 12, 31, 23, 30), (2026, 12, 1), 31),
            ("闰年 2 月 29 日", (2024, 2, 29, 12, 0), (2024, 2, 1), 29),
        ):
            now = calendar.timegm((*moment, 0, 0, 0, 0))
            started = calendar.timegm((*expected, 0, 0, 0, 0, 0))
            query = parse_qs(codex._cost_query(now))
            with self.subTest(case=label):
                self.assertEqual(codex._month_start(now), started)
                self.assertEqual(int(query["start_time"][0]), started)
                self.assertEqual(int(query["limit"][0]), days)
                self.assertLessEqual(int(query["limit"][0]), 180)

    def test_monthly_limit_failures_do_not_hide_spend(self):
        """上限缺权限或其他失败只追加一行，已花费结果必须保留。"""
        settings = codex.Settings(openai_usage_api_key="fixture-usage")
        costs = {
            "data": [{"start_time": NOW, "results": [{"amount": {"value": 0.12}}]}]
        }
        for error, expected in (
            (
                codex.UsageError(
                    "月度上限需 api.management.read 权限（Organization Admin key）"
                ),
                "api.management.read",
            ),
            (codex.UsageError("连接月度上限接口失败或超时"), "月度上限查询失败"),
        ):
            codex._reset_usage_cache()
            with (
                self.subTest(expected=expected),
                patch.object(codex, "fetch_openai_costs", return_value=costs),
                patch.object(codex, "fetch_openai_spend_limit", side_effect=error),
            ):
                text = codex.openai_usage_section(settings)
            self.assertIn("合计：$0.12", text)
            self.assertIn(expected, text)

    def test_missing_usage_scope_gives_actionable_hint(self):
        """403 缺 api.usage.read 时只给一行可执行提示，不整段抛 403。"""
        error = urllib.error.HTTPError(
            codex.OPENAI_COSTS_URL,
            403,
            "Forbidden",
            {},
            # 组织接口实际返回的是 {"error": "一句话"}，也要能识别缺失的 scope。
            io.BytesIO(
                json.dumps(
                    {
                        "error": "You have insufficient permissions for this operation. "
                        "Missing scopes: api.usage.read."
                    }
                ).encode()
            ),
        )
        hint = str(codex._usage_http_error(error))
        self.assertIn("api.usage.read", hint)
        self.assertIn("Admin key", hint)
        self.assertNotIn("HTTP 403", hint)

        # 上限接口缺 api.management.read 与「未配置上限」各有明确提示，都不是 404 噪音。
        management = urllib.error.HTTPError(
            codex.OPENAI_SPEND_LIMIT_URL,
            403,
            "Forbidden",
            {},
            io.BytesIO(
                json.dumps({"error": "Missing scopes: api.management.read."}).encode()
            ),
        )
        self.assertIn("api.management.read", str(codex._usage_http_error(management)))
        missing_limit = urllib.error.HTTPError(
            codex.OPENAI_SPEND_LIMIT_URL,
            404,
            "Not Found",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "error": {
                            "message": "No organization spend limit is configured.",
                            "code": "not_found",
                        }
                    }
                ).encode()
            ),
        )
        self.assertIn(
            "组织未设置月度预算上限", str(codex._usage_http_error(missing_limit))
        )

        settings = codex.Settings(openai_usage_api_key="fixture-usage")
        with patch.object(
            codex, "fetch_openai_costs", side_effect=codex.UsageError(hint)
        ):
            usage = codex.openai_usage_section(settings)
        # 订阅额度段照常展示，用量段只追加失败原因。
        combined = "订阅额度示例" + "\n\n" + usage
        self.assertIn("订阅额度示例", combined)
        self.assertIn("api.usage.read", combined)


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
            "/codex retry abcdef12",
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

        # retry 必须指定失败任务，不能猜测待确认草稿；带 image 前缀同样进入重试。
        self.thread.reset_mock()
        self.msg.content = "/codex retry"
        codex.on_ica_message(self.msg, self.client)
        self.thread.assert_not_called()
        self.msg.content = "/codex image retry abcdef12"
        codex.on_ica_message(self.msg, self.client)
        self.assertEqual(self.thread.call_args.kwargs["args"][1:2], ("retry",))

    def test_image_model_and_max_options_reach_the_worker(self):
        # 保护真实命令入口：选项不进入提示词，--max 无论顺序都固定选择 Sunburst/max。
        cases = (
            (
                "/codex image --model flare 一只猫",
                "create",
                "一只猫",
                {"model": "flare"},
            ),
            (
                "/codex direct --max 一只猫 --model flare --quality low",
                "direct",
                "一只猫",
                {"model": "sunburst", "quality": "max"},
            ),
            (
                "/codex set abcdef12 --model flare --quality low --max",
                "set",
                "abcdef12",
                {"model": "sunburst", "quality": "max"},
            ),
        )
        for command, action, value, options in cases:
            with self.subTest(command=command):
                self.thread.reset_mock()
                self.msg.content = command
                codex.on_ica_message(self.msg, self.client)
                args = self.thread.call_args.kwargs["args"]
                self.assertEqual((args[1], args[4], args[5]), (action, value, options))
        self.thread.reset_mock()
        self.msg.content = "/codex image --model gpt-image-2 一只猫"
        codex.on_ica_message(self.msg, self.client)
        self.thread.assert_not_called()

    def test_image_worker_reads_host_status_admins_to_delegate_in_current_room(self):
        # 宿主只有 status.admins；宽松 Mock 会把错误的 client.admins 也伪造成可用属性。
        msg = SimpleNamespace(
            id="fixture-admin-command",
            room_id=-123456,
            sender_id=10003,
            reply_with=lambda text: text,
        )
        client = SimpleNamespace(
            status=SimpleNamespace(self_id=10001, admins=[10003]),
            send_message=Mock(return_value=True),
        )
        archive = Mock()
        archive.resolve_job.return_value = {
            "id": "abcdef12-3456-7890-abcd-ef1234567890",
            "sender_id": 10004,
        }
        with (
            patch.dict(
                sys.modules,
                {
                    "codex_support.archive": archive_module,
                    "codex_support.image_service": image_service,
                },
            ),
            patch.object(
                image_service.ImageService,
                "archive",
                new_callable=PropertyMock,
                return_value=archive,
            ),
        ):
            codex._image_worker(self.state, "cancel", msg, client, "abcdef12", {})
            archive.cancel.assert_called_once_with(
                archive.resolve_job.return_value["id"], (10001, -123456, None)
            )
            archive.reset_mock()
            # 同一入口下，普通用户仍按本人限定，不能借代办修复扩大权限。
            client.status.admins = []
            codex._image_worker(self.state, "cancel", msg, client, "abcdef12", {})
            archive.cancel.assert_called_once_with(
                archive.resolve_job.return_value["id"], (10001, -123456, 10003)
            )

    def test_check_queries_subscription_even_when_image_backend_is_openai(self):
        """后端切换不影响订阅额度查询：API 费用不在这里查，但额度仍要能看。"""
        settings = codex.Settings(frozenset({123456}), image_backend="openai")
        with (
            patch.object(codex, "_runtime", codex.Runtime(settings)),
            patch.object(
                codex, "query_subscription", return_value="虚构订阅额度"
            ) as query,
        ):
            codex.on_ica_message(self.msg, self.client)
            self.assertEqual(self.thread.call_args.kwargs["name"], "codex-query")
            self.finish()
            query.assert_called_once()
        self.assertIn("虚构订阅额度", self.client.send_message.call_args.args[0])

    def test_usage_failure_does_not_swallow_subscription_result(self):
        """用量段异常只能追加一行失败，订阅额度结果必须照常回给群。"""
        with (
            patch.object(codex, "query_subscription", return_value="虚构订阅额度"),
            patch.object(
                codex, "openai_usage_section", side_effect=RuntimeError("boom")
            ),
        ):
            codex.on_ica_message(self.msg, self.client)
            self.finish()
        text = self.client.send_message.call_args.args[0]
        self.assertIn("虚构订阅额度", text)
        self.assertIn("OpenAI API 用量：本轮查询失败", text)

    def test_help_is_local_during_query_and_respects_group_boundary(self):
        self.msg.content = "/codex help"
        self.msg.room_id = -654321
        codex.on_ica_message(self.msg, self.client)
        self.client.send_message.assert_not_called()
        self.msg.room_id = -123456
        self.state.busy = True
        self.state.next_query_at = float("inf")
        for command, expected in (
            ("/codex", codex.HELP_TEXT),
            ("/codex help", codex.HELP_TEXT),
            ("/codex image", codex.IMAGE_HELP),
            ("/codex image help", codex.IMAGE_DETAILED_HELP),
        ):
            self.msg.content = command
            codex.on_ica_message(self.msg, self.client)
            self.assertEqual(self.client.send_message.call_args.args[0], expected)
        self.assertEqual(self.client.send_message.call_count, 4)
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
