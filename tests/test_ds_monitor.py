import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins import ds_monitor


class TargetConfigTests(unittest.TestCase):
    def test_official_uses_rust_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.object(
            ds_monitor, "load_rust_config", return_value={}
        ), patch.object(ds_monitor, "work_dir", return_value=temp):
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
        with tempfile.TemporaryDirectory() as temp, patch.object(
            ds_monitor, "load_rust_config", return_value=config
        ), patch.object(ds_monitor, "work_dir", return_value=temp):
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
            ds_monitor.new_archive_urls(None, [("platform", "new")], "https://archive.example"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
