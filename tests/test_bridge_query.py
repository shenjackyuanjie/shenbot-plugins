from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from plugins import bridge_query


@dataclass
class FakeReply:
    source_id: str
    content: str


@dataclass
class FakeMessage:
    content: str
    id: str = "command"
    room_id: int = -42
    sender_id: int = 101
    sender_name: str = "测试发送者"
    is_from_self: bool = False
    is_reply: bool = False
    files: list[dict] = field(default_factory=list)
    at: bool | str | None = None
    code: object = None
    time: int | None = 0
    reply: dict | None = None

    def reply_with(self, content: str) -> FakeReply:
        return FakeReply(self.id, content)


class FakeClient:
    def __init__(self, records: list[FakeMessage] | None = None) -> None:
        self.status = SimpleNamespace(admins=[101])
        self.records = records or []
        self.calls: list[tuple] = []
        self.sent: list[FakeReply] = []
        self.warnings: list[str] = []
        self.query_error: RuntimeError | None = None
        self.send_accepted = True

    def fetch_messages(
        self, room_id: int, options: dict | None = None, timeout: float = 15.0
    ) -> list[FakeMessage]:
        self.calls.append(("fetch_messages", room_id, options, timeout))
        if self.query_error:
            raise self.query_error
        return self.records

    def search_messages(
        self,
        room_id: int,
        keyword: str,
        offset: int = 0,
        sender_id: int | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        timeout: float = 15.0,
    ) -> list[FakeMessage]:
        self.calls.append(
            (
                "search_messages",
                room_id,
                keyword,
                offset,
                sender_id,
                start_time,
                end_time,
                timeout,
            )
        )
        if self.query_error:
            raise self.query_error
        return self.records

    def send_message(self, message: FakeReply) -> bool:
        self.sent.append(message)
        return self.send_accepted

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class BridgeQueryTests(unittest.TestCase):
    def test_non_admin_and_self_commands_are_quiet(self) -> None:
        client = FakeClient()
        for command in ("/bot-history", "/bot-search 测试", "/bot-media"):
            bridge_query.on_ica_message(FakeMessage(command, sender_id=202), client)
            bridge_query.on_ica_message(FakeMessage(command, is_from_self=True), client)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.sent, [])

    def test_history_is_current_room_only_and_sends_one_summary_not_history_messages(
        self,
    ) -> None:
        records = [
            FakeMessage(f"历史 {index}", id=f"history-{index}") for index in range(8)
        ]
        client = FakeClient(records)
        command = FakeMessage("/bot-history 2", room_id=-73)
        bridge_query.on_ica_message(command, client)
        self.assertEqual(client.calls, [("fetch_messages", -73, None, 15.0)])
        self.assertEqual(len(client.sent), 1)
        summary = client.sent[0]
        self.assertEqual(summary.source_id, "command")
        self.assertIn("本页 8 条，展示末尾 2 条", summary.content)
        self.assertIn("历史 6", summary.content)
        self.assertIn("历史 7", summary.content)
        self.assertNotIn("历史 5", summary.content)
        self.assertTrue(all(isinstance(message, FakeReply) for message in client.sent))

    def test_search_keeps_keyword_and_uses_current_room_default_filters(self) -> None:
        client = FakeClient([FakeMessage(f"匹配 {index}") for index in range(8)])
        bridge_query.on_ica_message(
            FakeMessage("/bot-search  中文  关键词  ", room_id=73), client
        )
        self.assertEqual(
            client.calls,
            [("search_messages", 73, "中文  关键词", 0, None, None, None, 15.0)],
        )
        self.assertEqual(len(client.sent), 1)
        text = client.sent[0].content
        self.assertIn("本页 8 条，展示 5 条", text)
        self.assertIn("匹配 0", text)
        self.assertNotIn("匹配 5", text)

    def test_media_reads_structured_reply_without_downloading_or_showing_resources(
        self,
    ) -> None:
        message = FakeMessage(
            "/bot-media",
            is_reply=True,
            reply={
                "content": "附件正文",
                "files": [
                    {"type": "image/jpeg", "url": "https://example.test/private"},
                    {"type": "image/png", "fid": "private-fid"},
                    {"type": "audio/ogg"},
                    {"type": "video/mp4"},
                    {"type": "application/pdf"},
                ],
            },
        )
        client = FakeClient()
        bridge_query.on_ica_message(message, client)
        self.assertEqual(client.calls, [])
        self.assertEqual(
            client.sent[0].content,
            "回复消息媒体：图片 2、语音 1、视频 1、文件 1\n正文摘要：附件正文",
        )
        self.assertNotIn("private", client.sent[0].content)

    def test_query_failure_does_not_expose_remote_exception_or_history(self) -> None:
        for command in ("/bot-history", "/bot-search 示例"):
            client = FakeClient([FakeMessage("不应发送的历史")])
            client.query_error = RuntimeError("private-token-and-payload")
            bridge_query.on_ica_message(FakeMessage(command), client)
            self.assertEqual(len(client.sent), 1)
            self.assertIn("查询失败", client.sent[0].content)
            self.assertNotIn("private-token", client.sent[0].content)
            self.assertNotIn("private-token", "".join(client.warnings))
            self.assertNotIn("不应发送的历史", client.sent[0].content)

    def test_summary_escapes_protocol_markup_instead_of_replaying_media_nodes(
        self,
    ) -> None:
        record = FakeMessage("[CQ:image,file=remote] [Face:1] <xml>card</xml>")
        client = FakeClient([record])
        bridge_query.on_ica_message(FakeMessage("/bot-history"), client)
        text = client.sent[0].content
        self.assertIn("［CQ:image,file=remote］", text)
        self.assertIn("＜xml＞card＜/xml＞", text)
        self.assertNotIn("[CQ:", text)
        self.assertNotIn("[Face:", text)


if __name__ == "__main__":
    unittest.main()
