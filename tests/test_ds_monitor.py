import json
import tempfile
import unittest
from pathlib import Path

from plugins import ds_monitor


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
