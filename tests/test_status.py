from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from plugins import status


class StatusWindowTests(unittest.TestCase):
    def test_query_refreshes_inactive_platform_in_both_directions(self) -> None:
        for update_name, hook in (
            ("tc_update", status.on_ica_message),
            ("ica_update", status.on_tailchat_message),
        ):
            with self.subTest(update=update_name):
                counter = status.Counter(120)
                with patch.object(status.time, "time", return_value=1000):
                    for _ in range(10):
                        getattr(counter, update_name)()
                message = Mock(content="/bot-count", is_from_self=False, is_reply=False)
                message.reply_with.side_effect = lambda text: text
                client = Mock()
                with (
                    patch.object(status, "COUNTER", counter),
                    patch.object(status.time, "time", return_value=1300),
                ):
                    hook(message, client)
                text = client.send_message.call_args.args[0]
                self.assertIn("0.00", text)
                self.assertIn("0.50", text)
                self.assertEqual(counter.tc_frequence + counter.ica_frequence, 0.5)

    def test_refresh_keeps_messages_at_window_boundary(self) -> None:
        counter = status.Counter(120)
        counter.ica_msg = [999, 1000, 1100]
        counter.tc_msg = [900, 1120]
        with patch.object(status.time, "time", return_value=1120):
            counter.refresh()
        self.assertEqual(counter.ica_frequence, 1.0)
        self.assertEqual(counter.tc_frequence, 0.5)


if __name__ == "__main__":
    unittest.main()
