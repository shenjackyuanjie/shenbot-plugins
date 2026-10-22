from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from plugins import sr_info


class SrInfoCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.message = Mock(is_from_self=False, is_reply=False, is_room_msg=True)
        self.message.reply_with.side_effect = lambda text: text
        self.client = Mock()

    def test_malformed_info_command_returns_usage_without_http(self) -> None:
        for command in ("/sr info123456", "/sr info", "/sr info 123 extra"):
            with self.subTest(command=command):
                self.message.content = command
                with patch.object(sr_info.requests, "get") as request:
                    sr_info.on_ica_message(self.message, self.client)
                request.assert_not_called()
                self.assertIn("用法", self.client.send_message.call_args.args[0])

    def test_valid_id_is_preserved_with_whitespace(self) -> None:
        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {
                "save_id": 123456,
                "save_type": "ship",
                "len": 1024,
                "blake_hash": "synthetic",
            },
        }
        self.message.content = "/sr info  123456  "
        with (
            patch.object(sr_info, "API_URL", "https://example.invalid", create=True),
            patch.object(sr_info.requests, "get", return_value=response) as request,
        ):
            sr_info.on_ica_message(self.message, self.client)
        request.assert_called_once_with(
            "https://example.invalid/info/123456", timeout=5
        )
        self.assertIn("ID: 123456", self.client.send_message.call_args.args[0])

    def test_non_numeric_id_returns_feedback_without_http(self) -> None:
        self.message.content = "/sr info abc"
        with patch.object(sr_info.requests, "get") as request:
            sr_info.on_ica_message(self.message, self.client)
        request.assert_not_called()
        self.client.send_message.assert_called_with("ID 必须是数字")


if __name__ == "__main__":
    unittest.main()
