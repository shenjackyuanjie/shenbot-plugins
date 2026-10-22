from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from plugins import hm_market


class HmMarketSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = {
            "name": "测试应用",
            "version": "1.2.3",
            "pkg_name": "com.example.test",
            "app_id": "C123456",
            "developer_name": "测试开发者",
            "main_device_codes": [0, 4],
            "download_count": 12345,
            "size_bytes": 1048576,
            "target_sdk": 12,
            "release_date": "2026-10-22",
        }

    def test_single_result_uses_pkg_summary_for_supported_response_shapes(self) -> None:
        responses = [
            {"data": [self.app], "total": 1},
            {"data": {"data": [self.app], "total_count": "1"}},
            {"data": [self.app]},
        ]
        for response in responses:
            with self.subTest(response=response):
                text = hm_market._format_search_result(response, "测试")
                self.assertEqual(
                    text,
                    hm_market.format_data(
                        {"data": {"full_info": self.app, "cached": True}}
                    ),
                )
                self.assertIn("刷新状态: 缓存数据", text)
                self.assertIn("名称/版本: 测试应用 1.2.3", text)
                self.assertIn("终端类型: 手机(0)、平板(4)", text)
                self.assertIn("大小: 1.00 MB", text)
                self.assertIn("SDK: 目标 SDK 12", text)
                self.assertIn("更新时间: 2026-10-22", text)
                self.assertNotIn("(1)", text)
                self.assertNotIn("共匹配", text)

    def test_multiple_results_keep_search_list(self) -> None:
        second_app = {**self.app, "name": "另一个应用"}
        response = {"data": [self.app, second_app], "total": 2}
        text = hm_market._format_search_result(response, "测试")
        self.assertIn("共匹配 2 项，显示前 2 项", text)
        self.assertIn("(1) 测试应用", text)
        self.assertIn("(2) 另一个应用", text)
        self.assertNotIn("刷新状态", text)

    def test_single_visible_result_with_more_matches_keeps_list(self) -> None:
        response = {"data": {"data": [self.app], "total_count": 6}}
        text = hm_market._format_search_result(response, "测试")
        self.assertIn("共匹配 6 项，显示前 1 项", text)
        self.assertIn("(1) 测试应用", text)

    def test_empty_results_keep_not_found_message(self) -> None:
        self.assertEqual(
            hm_market._format_search_result({"data": []}, "测试"),
            "未找到与“测试”匹配的应用",
        )

    def test_search_command_sends_single_summary_without_extra_request(self) -> None:
        message = Mock(content="/hm search 测试", is_from_self=False, is_reply=False)
        message.reply_with.side_effect = lambda text: text
        client = Mock()
        with patch.object(
            hm_market, "request_api", return_value={"data": [self.app], "total": 1}
        ) as request:
            hm_market.on_ica_message(message, client)
        request.assert_called_once()
        self.assertEqual(request.call_args.args, ("apps/query", "POST"))
        self.assertEqual(request.call_args.kwargs["params"]["detail"], "true")
        client.send_message.assert_called_once()
        self.assertIn(
            "名称/版本: 测试应用 1.2.3", client.send_message.call_args.args[0]
        )

    def test_failed_search_keeps_error_message(self) -> None:
        message = Mock()
        message.reply_with.side_effect = lambda text: text
        client = Mock()
        with patch.object(hm_market, "request_api", return_value=None):
            hm_market.query_search(message, client, "测试")
        client.send_message.assert_called_once_with("搜索失败，请稍后重试")


class HmMarketQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.message = Mock(is_from_self=False, is_reply=False)
        self.message.reply_with.side_effect = lambda text: text
        self.client = Mock()

    def test_market_links_choose_app_id_or_package(self) -> None:
        for identifier, method in (
            ("C123456", "app_id"),
            ("c123456", "app_id"),
            ("com.example.app", "pkg_name"),
            ("C123.example", "pkg_name"),
        ):
            with self.subTest(identifier=identifier):
                self.message.content = hm_market.MARKET_PREFIX + identifier
                with patch.object(hm_market, "query_pkg") as query:
                    hm_market.on_ica_message(self.message, self.client)
                query.assert_called_once_with(
                    self.message, self.client, identifier, method
                )

    def test_trend_package_with_app_id_prefix_does_not_resolve_as_id(self) -> None:
        with patch.object(hm_market, "request_api") as request:
            self.assertEqual(
                hm_market._resolve_pkg_name("C123.example"), "C123.example"
            )
        request.assert_not_called()

    def test_rankings_preserve_each_successful_group(self) -> None:
        good = {"data": [{"name": "可用应用", "download_count": 10000}]}
        for handler in (hm_market.query_top, hm_market.query_up):
            for bad, status in ((None, "查询失败"), ({"data": []}, "暂无数据")):
                for responses in ([good, bad], [bad, good]):
                    with self.subTest(handler=handler.__name__, responses=responses):
                        with patch.object(
                            hm_market, "request_api", side_effect=responses
                        ):
                            handler(self.message, self.client)
                        text = self.client.send_message.call_args.args[0]
                        self.assertIn("可用应用", text)
                        self.assertIn(status, text)

    def test_rankings_report_both_failures_without_crashing(self) -> None:
        for handler in (hm_market.query_top, hm_market.query_up):
            with patch.object(hm_market, "request_api", return_value=None):
                handler(self.message, self.client)
            self.assertEqual(
                self.client.send_message.call_args.args[0].count("查询失败"), 2
            )

    def test_search_suggests_refining_only_when_results_are_omitted(self) -> None:
        app = {"name": "测试应用"}
        text = hm_market._format_search_result({"data": [app, app], "total": 8}, "测试")
        self.assertIn("缩小范围", text)
        text = hm_market._format_search_result({"data": [app, app], "total": 2}, "测试")
        self.assertNotIn("缩小范围", text)


if __name__ == "__main__":
    unittest.main()
