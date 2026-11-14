from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plugins import hyp_vote


class HypVoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = patch.object(hyp_vote, "VOTE", {})
        self.state.start()
        self.addCleanup(self.state.stop)
        self.message = Mock(
            room_id=-1,
            sender_id=200,
            sender_name="测试用户",
            is_from_self=False,
            is_reply=False,
        )
        self.message.reply_with.side_effect = lambda text: text
        self.client = Mock(status=SimpleNamespace(admins=[100]))

    def run_command(self, content: str) -> None:
        self.message.content = content
        hyp_vote.on_ica_message(self.message, self.client)

    def test_non_admin_cannot_clear_votes(self) -> None:
        self.run_command("/hyp vote 0")
        self.run_command("/hyp clear")
        self.assertEqual(hyp_vote.room_votes(-1)[0], {200: "测试用户"})

    def test_admin_clear_only_affects_current_room(self) -> None:
        self.run_command("/hyp vote 0")
        hyp_vote.room_votes(-2)[12][300] = "其他用户"
        self.message.sender_id = 100
        self.run_command("/hyp clear")
        self.assertFalse(any(hyp_vote.room_votes(-1).values()))
        self.assertEqual(hyp_vote.room_votes(-2)[12], {300: "其他用户"})


if __name__ == "__main__":
    unittest.main()
