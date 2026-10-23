import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from plugins import ds_monitor


class ArchiveLinkTests(unittest.TestCase):
    def test_new_archive_urls_only_returns_new_records(self) -> None:
        before = [("platform", "old")]
        after = [
            ("platform", "new"),
            ("platform", "old"),
            ("fingerprints", "2026-09-28T00:00:00Z"),
        ]

        self.assertEqual(
            ds_monitor.new_archive_urls(before, after, "https://archive.example/"),
            [
                "https://archive.example/platform/#/platform/new",
                "https://archive.example/fingerprints/#/fingerprints/"
                "2026-09-28T00%3A00%3A00Z",
            ],
        )


class RecentChangeDisplayTests(unittest.TestCase):
    def make_target(self, output: str, key: str = "docs"):
        return ds_monitor.MonitorTarget(
            key, "测试目标", "https://example.invalid", output
        )

    def write_manifest(self, root: str, count: int) -> None:
        changes = Path(root, "changes", "batch-1")
        changes.mkdir(parents=True)
        entries = [
            {
                "kind": "updated",
                "title": f"文档-{index}",
                "url": f"https://example.invalid/docs/{index}",
            }
            for index in range(count)
        ]
        (changes / "manifest.json").write_text(
            json.dumps({"timestamp": "2026-10-23", "changes": entries}),
            encoding="utf-8",
        )

    def test_docs_list_has_total_and_retrievable_ninth_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.write_manifest(temp, 9)
            target = self.make_target(temp)
            first = ds_monitor.latest_docs_change_report(target)
            second = ds_monitor.latest_docs_change_report(target, 2)
        self.assertIn("共 9 项，第 1/2 页，显示 8 项", first)
        self.assertIn("后续还有 1 项", first)
        self.assertIn("/monitor last docs 2", first)
        self.assertNotIn("文档-8", first)
        self.assertIn("文档-8", second)
        self.assertNotIn("文档-0", second)
        self.assertIn("/monitor last docs 1", second)
        self.assertNotIn("下一页", second)

    def test_last_command_routes_pages_without_blocking_continuation(self) -> None:
        message = Mock(is_from_self=False, is_reply=False)
        message.reply_with.side_effect = lambda text: text
        client = Mock()
        with (
            patch.object(ds_monitor, "_client", None),
            patch.object(
                ds_monitor, "latest_change_report", return_value="测试摘要"
            ) as report,
        ):
            for page in (1, 2):
                message.content = f"/monitor last docs {page}"
                ds_monitor.on_ica_message(message, client)
            self.assertEqual(
                [call.args for call in report.call_args_list],
                [("docs", 1), ("docs", 2)],
            )
        self.assertEqual(client.send_message.call_count, 2)


if __name__ == "__main__":
    unittest.main()
