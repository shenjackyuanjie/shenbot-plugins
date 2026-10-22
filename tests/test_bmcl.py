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

    def test_local_help_does_not_request_backend(self) -> None:
        for command in ("/brrs", "/brrs  ", "/bmcl help", "/bmcl rankXYZ"):
            with self.subTest(command=command):
                self.message.content = command
                with patch.object(bmcl, "wrap_request") as request:
                    bmcl.on_ica_message(self.message, self.client)
                request.assert_not_called()
                self.client.send_message.assert_called_with(bmcl.HELP_MSG)

    def test_partial_prefix_does_not_trigger_request_or_reply(self) -> None:
        for command in ("/brrsXYZ Alpha", "/bmclXYZ"):
            self.message.content = command
            with patch.object(bmcl, "wrap_request") as request:
                bmcl.on_ica_message(self.message, self.client)
            request.assert_not_called()
        self.client.send_message.assert_not_called()

    def test_cold_dashboard_requests_version_only_once(self) -> None:
        dashboard = {
            "bytes": 1024,
            "hits": 10,
            "currentBandwidth": 1,
            "load": 0.1,
            "currentNodes": 1,
            "bandwidth": 10,
        }
        version = {"version": "test", "_resolved": "test#12345678"}
        self.message.content = "/bmcl"
        with (
            patch.object(bmcl, "backend_version", "unknown"),
            patch.object(
                bmcl, "wrap_request", side_effect=[dashboard, version]
            ) as request,
        ):
            bmcl.on_ica_message(self.message, self.client)
        self.assertEqual(
            [call.args[0].rsplit("/", 1)[1] for call in request.call_args_list],
            ["dashboard", "version"],
        )
        self.client.send_message.assert_called_once()
        self.assertIn("api版本 test", self.client.send_message.call_args.args[0])

    def test_general_rank_still_routes_without_name(self) -> None:
        self.message.content = "/bmcl rank"
        with (
            patch.object(bmcl, "ensure_backend_version", return_value=True),
            patch.object(bmcl, "bmcl_rank_general") as query,
        ):
            bmcl.on_ica_message(self.message, self.client)
        query.assert_called_once_with(self.message, self.client)


if __name__ == "__main__":
    unittest.main()
