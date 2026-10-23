from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from plugins import bmcl


class BmclCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.message = Mock(is_from_self=False, is_reply=False)
        self.message.reply_with.side_effect = lambda text: text
        self.client = Mock()

    def test_rank_alias_preserves_multiword_names_and_whitespace(self) -> None:
        for command in (
            "/brrs Alpha Beta",
            "/brrs  Alpha Beta",
            "/brrs\tAlpha Beta",
            "/bmcl rank Alpha Beta",
        ):
            with self.subTest(command=command):
                self.message.content = command
                with (
                    patch.object(bmcl, "ensure_backend_version", return_value=True),
                    patch.object(bmcl, "bmcl_rank") as query,
                ):
                    bmcl.on_ica_message(self.message, self.client)
                query.assert_called_once_with(self.message, self.client, "Alpha Beta")

    def test_empty_rank_with_successful_http_status_is_a_normal_result(self) -> None:
        response = Mock(status_code=200, reason=None)
        response.json.return_value = []
        self.message.content = "/bmcl rank"
        with (
            patch.object(bmcl, "backend_version", "known"),
            patch.object(bmcl.requests, "get", return_value=response),
        ):
            bmcl.on_ica_message(self.message, self.client)
        self.client.send_message.assert_called_once()
        self.assertIn("暂无节点", self.client.send_message.call_args.args[0])
        self.client.send_and_warn.assert_not_called()

    def test_failed_dashboard_stops_after_one_request_without_exposing_exception(
        self,
    ) -> None:
        self.message.content = "/bmcl"
        with patch.object(
            bmcl.requests,
            "get",
            side_effect=bmcl.requests.ConnectionError("fixture-private-cookie"),
        ) as request:
            bmcl.on_ica_message(self.message, self.client)
        self.assertNotIn("fixture-private-cookie", str(self.client.mock_calls))
        request.assert_called_once()
        self.client.send_and_warn.assert_called_once()

    def test_partial_prefix_does_not_trigger_request_or_reply(self) -> None:
        for command in ("/brrsXYZ Alpha", "/bmclXYZ"):
            self.message.content = command
            with patch.object(bmcl, "wrap_request") as request:
                bmcl.on_ica_message(self.message, self.client)
            request.assert_not_called()
        self.client.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
