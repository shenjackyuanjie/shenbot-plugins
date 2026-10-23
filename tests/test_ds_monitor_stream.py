"""验证 stdout 消费和损坏归档隔离，不运行监测进程。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from plugins import ds_monitor as monitor


RESULT = (
    "=== 本轮检查结果 changes=1 notices=0 errors=0 "
    "chat=change official=nochange harness=nochange platform=nochange "
    "docs=nochange fingerprint=nochange status=nochange ==="
)
DONE = "=== 本轮完成 changes=1 fingerprint=0 analyzed=1 ==="


class StreamAndArchiveTests(unittest.TestCase):
    def test_bad_lines_do_not_interrupt_reader_and_only_ordered_current_completions_publish(self):
        proc = Mock()
        proc.poll.return_value = None
        bad = [
            RESULT.replace("errors=0", f"errors={value}")
            for value in ("--1", "²", "-1", "9" * 100)
        ]
        bad += [RESULT.replace("chat=change", "chat=unknown"), RESULT + " extra",
                RESULT.replace("errors=0", "errors=0 errors=1")]
        proc.stdout = iter([
            DONE, *bad,
            "[Chat] " + DONE, RESULT, DONE, DONE,
            RESULT.replace("changes=1", "changes=0"),
            DONE.replace("changes=1 fingerprint=0", "changes=0 fingerprint=1"),
        ])
        with (
            patch.multiple(
                monitor, _watch_generation=42, _watch_process=proc,
                _watch_exited_generation=-1, _cycle_result_generation=-1,
                _cycle_done_pending_generation=-1, _cycle_statuses={}, _cycle_counts={},
                _enabled=True, _unloading=False, _watch_stop_requested=False,
                _publish_enabled=True, _target_configs={}, _active_output_target=None,
                _watch_summary_lines=None, _summary_owner=None,
            ),
            patch.object(monitor, "log"),
            patch.object(monitor, "maybe_publish") as publish,
        ):
            monitor.watch_stdout_loop(proc, 42)
            self.assertEqual([call.args for call in publish.call_args_list], [(1,), (1,)])
            self.assertIs(monitor._watch_process, proc)
            self.assertTrue(monitor.wait_cycle_result(42, 0))
            monitor.handle_output_line(RESULT, 41)
            monitor.handle_output_line(DONE, 41)
            self.assertEqual(publish.call_count, 2)
            self.assertEqual(monitor._cycle_counts["changes"], 0)

    def test_corrupt_archive_is_reported_without_hiding_other_targets_or_inventing_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            web = root / "chat/snapshot-1"
            docs = root / "docs/changes/batch-1"
            site = root / "site/data"
            for directory in (web, docs, site):
                directory.mkdir(parents=True)
            (web / "diff-from-previous.patch").write_text("+new\n-old", encoding="utf-8")
            metadata = web / "metadata.json"
            manifest = docs / "manifest.json"
            index = site / "site.json"
            targets = {
                "chat": monitor.MonitorTarget("chat", "Chat", "https://example.invalid", str(web.parent)),
                "docs": monitor.MonitorTarget("docs", "文档", "https://example.invalid", str(root / "docs")),
            }
            metadata.write_text("[]", encoding="utf-8")
            manifest.write_text(json.dumps({"changes": [{"kind": "updated", "title": "正常文档"}]}), encoding="utf-8")
            with patch.object(monitor, "_target_configs", targets):
                text = monitor.latest_change_report()
                self.assertIn("metadata 格式无效", text)
                self.assertIn("正常文档", text)
                metadata.write_text("{}", encoding="utf-8")
                for malformed in (b"[]", b'{"changes":[null]}', b'{"changes":{}}', b"\xff"):
                    manifest.write_bytes(malformed)
                    text = monitor.latest_change_report()
                    self.assertIn("Chat 最近一次网页修改", text)
                    self.assertTrue("格式无效" in text or "读取 manifest 失败" in text)
            for malformed in (
                b"[]", b"null", b"\xff", b'{"targets":[]}',
                b'{"targets":{"chat":[null]},"fingerprints":[]}',
                b'{"schema_version":7,"fingerprints":[]}',
                b'{"schema_version":7,"targets":{}}',
            ):
                index.write_bytes(malformed)
                unknown = monitor.archive_entries(str(root))
                self.assertIsNone(unknown)
                self.assertEqual(monitor.new_archive_urls(unknown, [("chat", "old")], "https://example.invalid"), [])

            history = root / "fingerprint-history.jsonl"
            valid = {"event": "change", "checked_at": "2026-10-24", "changes": [], "models": {}, "errors": ["连接失败"]}
            invalid = {**valid, "errors": ["连接失败", None]}
            history.write_text(json.dumps(valid) + "\n" + json.dumps(invalid) + "\n", encoding="utf-8")
            with patch.object(monitor, "_fingerprint_history_path", str(history)):
                self.assertIn("最近 1 条", monitor.fingerprint_history_report())
                history.write_bytes(b"\xff")
                self.assertIn("读取 fingerprint 历史失败", monitor.fingerprint_history_report())


if __name__ == "__main__":
    unittest.main()
