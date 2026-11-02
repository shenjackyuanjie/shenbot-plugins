"""CLI 分片流程：防漏算、重复计算，以及切换并行后重算已有成功。"""
import io
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from test_namer_history import FakeExecutor, HistoryMemory, namerena, recompute, sample

from tools import namer_history as cli


class CliMemory(HistoryMemory):
    def initialize(self):
        pass

    @contextmanager
    def db(self):
        yield SimpleNamespace(info=SimpleNamespace(dbname="namer"))


class ShardFlowTests(unittest.TestCase):
    def test_four_cli_shards_cover_each_source_once_and_keep_prior_success(self):
        messages = []
        for i in range(16):
            message = sample(f"/namer-peek\nfictional-{i}", "/namer-peek")
            message.message_id = f"synthetic-message-{i}"
            messages.append(message)
        archive, executor = CliMemory(), FakeExecutor()
        executor.snapshot = dict
        self.assertEqual(recompute(messages[0], archive, executor, {}), "succeeded")
        previous_calls = set(archive.calls)
        source = SimpleNamespace(iter_messages=lambda **kwargs: iter(messages))
        processed = []
        real_recompute = recompute

        def record(message, *args, **kwargs):
            processed.append(message.message_id)
            return real_recompute(message, *args, **kwargs)

        with (
            patch.dict(sys.modules, {"namerena": namerena}),
            patch("namerena_support.archive.Archive", return_value=archive),
            patch("namerena_support.history.EngineExecutor", return_value=executor),
            patch("namerena_support.history.recompute", side_effect=record),
            patch("namerena_support.history_source.HistorySource", return_value=source),
            patch.object(cli.Path, "read_text", return_value='[main]\npg_connection="dbname=synthetic"\n'),
            patch.object(cli.Path, "exists", return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            per_shard = []
            for index in range(4):
                before = len(processed)
                self.assertEqual(cli.main(["--group", "12345", "--execute", "--mode", "resume",
                    "--shard-count", "4", "--shard-index", str(index)]), 0)
                per_shard.append(len(processed) - before)

        self.assertTrue(all(per_shard))
        self.assertCountEqual(processed, [message.message_id for message in messages])
        self.assertEqual(len(executor.bodies), len(messages))
        self.assertTrue(previous_calls <= set(archive.calls))
        self.assertEqual(len(archive.jobs), len(messages))
        self.assertTrue(all(job["state"] == "succeeded" for job in archive.jobs.values()))
