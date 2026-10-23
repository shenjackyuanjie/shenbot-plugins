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

    def test_partial_prefix_does_not_trigger_request_or_reply(self) -> None:
        for command in ("/brrsXYZ Alpha", "/bmclXYZ"):
            self.message.content = command
            with patch.object(bmcl, "wrap_request") as request:
                bmcl.on_ica_message(self.message, self.client)
            request.assert_not_called()
        self.client.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
