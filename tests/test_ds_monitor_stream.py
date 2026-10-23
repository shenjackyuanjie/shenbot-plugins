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
    def test_bad_lines_do_not_interrupt_reader_and_only_ordered_current_completions_publish(
        self,
    ):
        proc = Mock()
        proc.poll.return_value = None
        proc.stdout = iter(
            [DONE, RESULT.replace("errors=0", "errors=-1"), RESULT, DONE, DONE]
        )
        with (
            patch.multiple(
                monitor,
                _watch_generation=42,
                _watch_process=proc,
                _watch_exited_generation=-1,
                _cycle_result_generation=-1,
                _cycle_done_pending_generation=-1,
                _cycle_statuses={},
                _cycle_counts={},
                _enabled=True,
                _unloading=False,
                _watch_stop_requested=False,
                _publish_enabled=True,
                _target_configs={},
                _active_output_target=None,
                _watch_summary_lines=None,
                _summary_owner=None,
            ),
            patch.object(monitor, "log"),
            patch.object(monitor, "maybe_publish") as publish,
        ):
            monitor.watch_stdout_loop(proc, 42)
            publish.assert_called_once_with(1)
            self.assertTrue(monitor.wait_cycle_result(42, 0))
            monitor.handle_output_line(RESULT, 41)
            monitor.handle_output_line(DONE, 41)
            publish.assert_called_once()

    def test_corrupt_archive_is_reported_without_hiding_other_targets_or_inventing_links(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            web = root / "chat/snapshot-1"
            docs = root / "docs/changes/batch-1"
            site = root / "site/data"
            for directory in (web, docs, site):
                directory.mkdir(parents=True)
            (web / "diff-from-previous.patch").write_text(
                "+new\n-old", encoding="utf-8"
            )
            (web / "metadata.json").write_text("[]", encoding="utf-8")
            (docs / "manifest.json").write_text(
                json.dumps({"changes": [{"kind": "updated", "title": "正常文档"}]}),
                encoding="utf-8",
            )
            targets = {
                "chat": monitor.MonitorTarget(
                    "chat", "Chat", "https://example.invalid", str(web.parent)
                ),
                "docs": monitor.MonitorTarget(
                    "docs", "文档", "https://example.invalid", str(root / "docs")
                ),
            }
            with patch.object(monitor, "_target_configs", targets):
                text = monitor.latest_change_report()
            self.assertIn("metadata 格式无效", text)
            self.assertIn("正常文档", text)
            (site / "site.json").write_text("[]", encoding="utf-8")
            unknown = monitor.archive_entries(str(root))
            self.assertIsNone(unknown)
            self.assertEqual(
                monitor.new_archive_urls(
                    unknown, [("chat", "old")], "https://example.invalid"
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
