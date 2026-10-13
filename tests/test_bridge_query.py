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
        self.calls.append(("search_messages", room_id, keyword, offset, sender_id, start_time, end_time, timeout))
        if self.query_error:
            raise self.query_error
        return self.records

    def send_message(self, message: FakeReply) -> bool:
        self.sent.append(message)
        return self.send_accepted

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class BridgeQueryTests(unittest.TestCase):
    def test_ordinary_message_and_partial_command_do_not_query_or_send(self) -> None:
        client = FakeClient()
        for content in ("普通消息", "", "/bot-searching 测试", "看一下 /bot-history"):
            bridge_query.on_ica_message(FakeMessage(content), client)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.sent, [])

    def test_non_admin_and_self_commands_are_quiet(self) -> None:
        client = FakeClient()
        for command in ("/bot-history", "/bot-search 测试", "/bot-media"):
            bridge_query.on_ica_message(FakeMessage(command, sender_id=202), client)
            bridge_query.on_ica_message(FakeMessage(command, is_from_self=True), client)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.sent, [])

    def test_history_is_current_room_only_and_sends_one_summary_not_history_messages(self) -> None:
        records = [FakeMessage(f"历史 {index}", id=f"history-{index}") for index in range(8)]
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

    def test_history_default_is_bounded_and_consumes_media_fields(self) -> None:
        record = FakeMessage(
            "x" * 500,
            sender_name="s" * 500,
            time=1000,
            files=[{"type": "image/png"}, {"type": "audio/ogg"}, {"type": "file"}],
            at=True,
            code={"resource": "resource-id"},
        )
        client = FakeClient([record] * 20)
        bridge_query.on_ica_message(FakeMessage("/bot-history"), client)
        text = client.sent[0].content
        self.assertIn("展示末尾 5 条", text)
        self.assertEqual(text.count("图片 1、语音 1、文件 1；提及当前账号；含协议附加数据"), 5)
        self.assertIn("1970-01-01 00:00:01 UTC", text)
        self.assertNotIn("resource-id", text)
        self.assertNotIn("x" * 80, text)
        self.assertLess(len(text), 1500)

    def test_received_at_is_account_or_all_marker_not_sender_array(self) -> None:
        for at, expected in ((True, "提及当前账号"), ("all", "提及全体成员")):
            with self.subTest(at=at):
                client = FakeClient([FakeMessage("正文", at=at)])
                bridge_query.on_ica_message(FakeMessage("/bot-history"), client)
                self.assertIn(expected, client.sent[0].content)
        for at in (False, None):
            with self.subTest(at=at):
                client = FakeClient([FakeMessage("正文", at=at)])
                bridge_query.on_ica_message(FakeMessage("/bot-history"), client)
                self.assertNotIn("提及", client.sent[0].content)

    def test_millisecond_timestamp_uses_utc_and_keeps_epoch_zero(self) -> None:
        for timestamp, expected in (
            (0, "1970-01-01 00:00:00 UTC"),
            (1000, "1970-01-01 00:00:01 UTC"),
            (None, "时间未知"),
            (10**20, "时间未知"),
        ):
            with self.subTest(timestamp=timestamp):
                client = FakeClient([FakeMessage("正文", time=timestamp)])
                bridge_query.on_ica_message(FakeMessage("/bot-history"), client)
                self.assertIn(expected, client.sent[0].content)

    def test_history_rejects_invalid_limit_without_query(self) -> None:
        for value in ("0", "21", "-1", "abc", "3 4"):
            with self.subTest(limit=value):
                client = FakeClient()
                bridge_query.on_ica_message(FakeMessage(f"/bot-history {value}"), client)
                self.assertEqual(client.calls, [])
                self.assertIn("N 为 1-20", client.sent[0].content)

    def test_search_keeps_keyword_and_uses_current_room_default_filters(self) -> None:
        client = FakeClient([FakeMessage(f"匹配 {index}") for index in range(8)])
        bridge_query.on_ica_message(FakeMessage("/bot-search  中文  关键词  ", room_id=73), client)
        self.assertEqual(client.calls, [("search_messages", 73, "中文  关键词", 0, None, None, None, 15.0)])
        self.assertEqual(len(client.sent), 1)
        text = client.sent[0].content
        self.assertIn("本页 8 条，展示 5 条", text)
        self.assertIn("匹配 0", text)
        self.assertNotIn("匹配 5", text)

    def test_search_requires_bounded_nonempty_keyword(self) -> None:
        for value in ("", "k" * 201):
            client = FakeClient()
            bridge_query.on_ica_message(FakeMessage(f"/bot-search {value}"), client)
            self.assertEqual(client.calls, [])
            self.assertIn("关键词为 1-200", client.sent[0].content)

    def test_empty_queries_are_not_reported_as_errors(self) -> None:
        for command, expected in (
            ("/bot-history", "暂无本地历史"),
            ("/bot-search 关键词", "没有匹配的本地消息"),
        ):
            client = FakeClient()
            bridge_query.on_ica_message(FakeMessage(command), client)
            self.assertIn(expected, client.sent[0].content)
            self.assertEqual(client.warnings, [])

    def test_media_reads_structured_reply_without_downloading_or_showing_resources(self) -> None:
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
        self.assertEqual(client.sent[0].content, "回复消息媒体：图片 2、语音 1、视频 1、文件 1\n正文摘要：附件正文")
        self.assertNotIn("private", client.sent[0].content)

    def test_media_accepts_legacy_single_file_and_skips_non_file_entries(self) -> None:
        for reply in (
            {"file": {"type": "image/png"}},
            {"files": [None, "not-a-file", {"type": "image/png"}]},
        ):
            client = FakeClient()
            bridge_query.on_ica_message(FakeMessage("/bot-media", is_reply=True, reply=reply), client)
            self.assertIn("图片 1", client.sent[0].content)
            self.assertEqual(client.calls, [])

    def test_media_requires_reply_and_no_arguments(self) -> None:
        for message in (
            FakeMessage("/bot-media"),
            FakeMessage("/bot-media extra", is_reply=True),
        ):
            client = FakeClient()
            bridge_query.on_ica_message(message, client)
            self.assertIn("请回复一条消息", client.sent[0].content)
            self.assertEqual(client.calls, [])

    def test_missing_reply_snapshot_is_distinct_from_no_attachments(self) -> None:
        client = FakeClient()
        bridge_query.on_ica_message(FakeMessage("/bot-media", is_reply=True), client)
        self.assertIn("未包含可用的原消息结构", client.sent[0].content)

    def test_old_host_can_keep_loading_plugin_and_receives_upgrade_hint_on_command(self) -> None:
        for command, method in (("/bot-history", "fetch_messages"), ("/bot-search 示例", "search_messages")):
            client = FakeClient()
            setattr(client, method, None)
            bridge_query.on_ica_message(FakeMessage(command), client)
            self.assertEqual(client.calls, [])
            self.assertIn("请先升级 Bot", client.sent[0].content)

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

    def test_summary_escapes_protocol_markup_instead_of_replaying_media_nodes(self) -> None:
        record = FakeMessage("[CQ:image,file=remote] [Face:1] <xml>card</xml>")
        client = FakeClient([record])
        bridge_query.on_ica_message(FakeMessage("/bot-history"), client)
        text = client.sent[0].content
        self.assertIn("［CQ:image,file=remote］", text)
        self.assertIn("＜xml＞card＜/xml＞", text)
        self.assertNotIn("[CQ:", text)
        self.assertNotIn("[Face:", text)

    def test_emit_failure_is_logged_without_claiming_qq_completion(self) -> None:
        client = FakeClient()
        client.send_accepted = False
        bridge_query.on_ica_message(FakeMessage("/bot-history"), client)
        self.assertEqual(client.warnings, ["bridge_query：摘要发送请求未受理"])


if __name__ == "__main__":
    unittest.main()
