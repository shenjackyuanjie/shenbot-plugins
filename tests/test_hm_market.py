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

    def test_rankings_preserve_each_successful_group(self) -> None:
        good = {"data": [{"name": "可用应用", "download_count": 10000}]}
        for handler in (hm_market.query_top, hm_market.query_up):
            with patch.object(hm_market, "request_api", side_effect=[good, None]):
                handler(self.message, self.client)
            text = self.client.send_message.call_args.args[0]
            self.assertIn("可用应用", text)
            self.assertIn("查询失败", text)


if __name__ == "__main__":
    unittest.main()
