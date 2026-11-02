"""防止源读取越群/越权、截断原输入及同毫秒分页漏样本；只使用虚构数据。"""
import unittest
from unittest.mock import Mock, patch

from plugins.namerena_support.history_source import HistorySource, SourceError


class HistorySourceTests(unittest.TestCase):
    def source(self):
        settings = patch("plugins.namerena_support.history_source.load_replica_settings",
                         return_value=("eqq000", {"conninfo": "mock-only"}))
        settings.start()
        self.addCleanup(settings.stop)
        return HistorySource(group=10001)

    def test_complete_inputs_and_same_millisecond_cursor_preserve_source_scope(self):
        """超长输入不截断、不删链接；固定群/可见过滤/只读事务在每页生效。"""
        source = self.source()
        first = {"message_id": "fiction-a", "sender_id": "fiction-user", "time_ms": 1700000000001,
                 "content": "/namer_peeks\n" + "X" * 5000 + " https://example.invalid/input"}
        second = {**first, "message_id": "fiction-b", "content": "/namer-rhp\nFictionalName"}
        third = {**first, "message_id": "fiction-c", "content": "/namer-unknown\nFictionalName"}
        db = Mock()
        db.__enter__ = Mock(return_value=db)
        db.__exit__ = Mock(return_value=False)
        results = [Mock(), Mock(), Mock()]
        results[0].fetchall.return_value = [first, second]
        results[1].fetchall.return_value = [third]
        db.execute.side_effect = results
        with patch("psycopg.connect", return_value=db) as connect:
            messages = list(source.iter_messages(2, before=(1700000000002, "fiction-z")))
        self.assertEqual([message.message_id for message in messages],
                         ["fiction-a", "fiction-b", "fiction-c"])
        self.assertEqual(messages[0].content, first["content"])
        self.assertEqual([message.command for message in messages], ["/namer-peeks", "/namer-fsp", None])
        self.assertIsNone(messages[0].account)
        self.assertEqual(messages[0].time_ms, first["time_ms"])
        self.assertNotIn("fiction-user", repr(messages[0]))
        self.assertNotIn("example.invalid", repr(messages[0]))
        second_params = db.execute.call_args_list[1].args[1]
        self.assertEqual(second_params[2:4], [first["time_ms"], "fiction-b"])
        for call in db.execute.call_args_list:
            query, params = call.args
            query = query.as_string()
            self.assertEqual(params[0], source.room_id)
            for protection in ("deleted", "hide", "reveal", "flash"):
                self.assertIn(protection + " IS NOT TRUE", query)
            for forbidden in ("replyMessage", "username", "LEFT(", "UPDATE", "INSERT"):
                self.assertNotIn(forbidden, query)
        for call in connect.call_args_list:
            options = call.kwargs["options"]
            self.assertIn("default_transaction_read_only=on", options)
            self.assertIn("search_path=pg_catalog", options)
            self.assertIn("statement_timeout=8000", options)
            self.assertIn("lock_timeout=1000", options)
        self.assertTrue(db.read_only)
        self.assertEqual(db.rollback.call_count, 2)

    def test_inventory_redacts_unknown_tokens_and_driver_failures(self):
        """未知命令 token 可含用户名字，统计不暴露它；驱动错误也不泄露源连接。"""
        source = self.source()
        db = Mock()
        db.__enter__ = Mock(return_value=db)
        db.__exit__ = Mock(return_value=False)
        db.execute.return_value.fetchall.return_value = [
            {"command": "/namer-private-fiction", "month": "2023-11", "count": 2,
             "first_ms": 1700000000000, "last_ms": 1700000001000},
            {"command": "/namer-pf", "month": "2023-11", "count": 3,
             "first_ms": 1700000002000, "last_ms": 1700000003000},
        ]
        with patch("psycopg.connect", return_value=db):
            result = source.inventory()
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["by_command"], {"unsupported": 2, "/namer-pf": 3})
        self.assertNotIn("private-fiction", repr(result))
        self.assertTrue(result["first_time"].endswith("+08:00"))
        with (patch("psycopg.connect", side_effect=RuntimeError("fictional private DSN")),
              self.assertRaisesRegex(SourceError, "^source_read_failed$")):
            source.inventory()
        with patch("psycopg.connect") as connect:
            with self.assertRaisesRegex(SourceError, "invalid_source_group"):
                HistorySource(group=0)
            with self.assertRaises(TypeError):
                HistorySource()
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
