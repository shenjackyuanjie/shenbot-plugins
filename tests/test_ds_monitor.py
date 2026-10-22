import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from plugins import ds_monitor


class TargetConfigTests(unittest.TestCase):
    def test_official_uses_rust_defaults(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(ds_monitor, "load_rust_config", return_value={}),
            patch.object(ds_monitor, "work_dir", return_value=temp),
        ):
            targets = ds_monitor.load_target_configs()

        self.assertEqual(tuple(targets), ds_monitor.WEB_TARGET_KEYS)
        self.assertEqual(targets["official"].label, "官网")
        self.assertEqual(targets["official"].url, "https://www.deepseek.com/")
        self.assertEqual(
            os.path.normpath(targets["official"].output),
            str(Path(temp, "output", "official")),
        )
        self.assertTrue(targets["official"].enabled)

    def test_official_config_can_be_overridden_and_disabled(self) -> None:
        config = {
            "official": {
                "enabled": False,
                "url": "https://example.test/official/",
                "output": "archive/official",
            }
        }
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(ds_monitor, "load_rust_config", return_value=config),
            patch.object(ds_monitor, "work_dir", return_value=temp),
        ):
            target = ds_monitor.load_target_configs()["official"]

        self.assertFalse(target.enabled)
        self.assertEqual(target.url, "https://example.test/official/")
        self.assertEqual(
            os.path.normpath(target.output), str(Path(temp, "archive", "official"))
        )

    def test_cycle_reporting_includes_official_failures(self) -> None:
        self.assertEqual(ds_monitor.CYCLE_TARGET_LABELS["official"], "官网")
        self.assertIn("official", ds_monitor.CYCLE_ERROR_KEYS)
        self.assertEqual(ds_monitor.TARGET_HELP, "chat|official|harness|platform|docs")


class ArchiveLinkTests(unittest.TestCase):
    def test_archive_entries_reads_targets_and_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp, "site", "data")
            data_dir.mkdir(parents=True)
            Path(data_dir, "site.json").write_text(
                json.dumps(
                    {
                        "targets": {
                            "platform": [{"id": "snapshot-2"}, {"id": "snapshot-1"}],
                            "docs": [{"id": "batch-1"}],
                        },
                        "fingerprints": [{"checked_at": "2026-09-28T00:00:00Z"}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                ds_monitor.archive_entries(temp),
                [
                    ("platform", "snapshot-2"),
                    ("platform", "snapshot-1"),
                    ("docs", "batch-1"),
                    ("fingerprints", "2026-09-28T00:00:00Z"),
                ],
            )

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

    def test_new_archive_urls_needs_both_indexes(self) -> None:
        self.assertEqual(
            ds_monitor.new_archive_urls(
                None, [("platform", "new")], "https://archive.example"
            ),
            [],
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

    def test_single_doc_includes_title_kind_and_direct_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.write_manifest(temp, 1)
            text = ds_monitor.latest_docs_change_report(self.make_target(temp))
        self.assertIn("[updated] 文档-0", text)
        self.assertIn("链接: https://example.invalid/docs/0", text)
        self.assertNotIn("下一页", text)

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

    def test_web_resources_are_not_discarded_before_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp, "snapshot-1")
            snapshot.mkdir()
            diff = "\n".join(
                f"=== RESOURCE UNIFIED DIFF: resource-{i} ===" for i in range(9)
            )
            (snapshot / "diff-from-previous.patch").write_text(diff, encoding="utf-8")
            target = self.make_target(temp, "chat")
            first = ds_monitor.latest_web_change_report(target)
            second = ds_monitor.latest_web_change_report(target, 2)
        self.assertIn("共 9 项", first)
        self.assertNotIn("resource-8", first)
        self.assertIn("/monitor last chat 2", first)
        self.assertIn("resource-8", second)

    def test_empty_and_out_of_range_pages_have_feedback(self) -> None:
        for count, page, expected in ((0, 1, "暂无变更条目"), (1, 2, "页码超出范围")):
            with (
                self.subTest(count=count, page=page),
                tempfile.TemporaryDirectory() as temp,
            ):
                self.write_manifest(temp, count)
                text = ds_monitor.latest_docs_change_report(
                    self.make_target(temp), page
                )
                self.assertIn(expected, text)

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

    def test_invalid_page_does_not_read_report(self) -> None:
        message = Mock()
        message.reply_with.side_effect = lambda text: text
        client = Mock()
        with patch.object(ds_monitor, "latest_change_report") as report:
            for page in ("0", "-1", "abc"):
                ds_monitor.cmd_last(message, client, "docs", page)
                self.assertIn("页码必须是正整数", client.send_message.call_args.args[0])
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
