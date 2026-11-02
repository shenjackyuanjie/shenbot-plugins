"""离线重算保护：原始输入保真、无消息客户端、失败和覆盖重试。"""
import copy
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from plugins import namerena

# isort: split
# 插件入口先建立导入根与本轮 support；顺序不能由排序器反转。
from namerena_support.archive import ArchiveError, HistoryWriter
from namerena_support.history import EngineExecutor, Settings, recompute
from test_namer_archive import MemoryArchive


class HistoryMemory(MemoryArchive):
    @contextmanager
    def history_job(self, source, payload, version, provenance, mode="overwrite"):
        key = (source["source_alias"], source["room_id"], source["message_id"])
        old = next((identity for identity, job in self.jobs.items() if job["key"] == key), None)
        if old and mode == "resume" and self.jobs[old]["state"] == "succeeded" and self.jobs[old]["provenance"]["fingerprint"] == provenance["fingerprint"]:
            yield None
            return
        identity = old or uuid4()
        self.calls = {key: call for key, call in self.calls.items() if call["job"] != identity}
        self.jobs[identity] = {"key": key, "source": source, "input": copy.deepcopy(payload),
                               "state": "started", "result": None, "provenance": provenance}
        self.events[identity] = threading.Event()
        self.identity = identity
        yield self


class FakeExecutor:
    module = namerena
    settings = Settings(rounds=1, pair_rounds=1)

    def __init__(self):
        self.fail = False
        self.bodies = []

    def verify(self):
        pass

    def execute(self, command, body, payload):
        self.bodies.append(body)
        session = namerena.CURRENT.get()
        handle = session.begin("python", "offline", {"groups": payload["groups"]})
        if self.fail:
            session.end(handle, error={"code": "synthetic_failure"})
        else:
            session.end(handle, {"version": 1, "kind": "attributes", "items": [
                {"input_index": i, "score": 5} for i in range(len(payload["groups"]))]})


def sample(content='/namer_peeks\n A+B[" ', command='/namer-peeks'):
    return SimpleNamespace(platform="ica", room_id="synthetic-room", message_id="synthetic-message",
        sender_id="synthetic-sender", source_alias="synthetic-source", account=None,
        requested_at=datetime(2024, 1, 1, tzinfo=UTC), content=content,
        command=command, raw_command=content.split("\n", 1)[0])


class HistoryTests(unittest.TestCase):
    def test_input_only_recompute_resumes_and_overwrites_old_success_with_explicit_failure(self):
        archive, executor = HistoryMemory(), FakeExecutor()
        value = sample()
        self.assertEqual(recompute(value, archive, executor, {}), "succeeded")
        identity = next(iter(archive.jobs))
        first_calls = set(archive.calls)
        self.assertEqual(executor.bodies, [' A+B[" '])
        self.assertEqual(recompute(value, archive, executor, {}, mode="resume"), "skipped")
        executor.fail = True
        self.assertEqual(recompute(value, archive, executor, {}, mode="overwrite"), "failed")
        self.assertEqual(list(archive.jobs), [identity])
        self.assertFalse(first_calls & set(archive.calls))
        self.assertEqual(archive.jobs[identity]["state"], "failed")
        self.assertEqual(archive.jobs[identity]["source"]["requested_at"].year, 2024)
        executor.fail = False
        self.assertEqual(recompute(value, archive, executor, {}, mode="retry-failed"), "succeeded")
        self.assertEqual(len(archive.calls), 1)

    def test_unsupported_or_missing_body_is_recorded_without_execution(self):
        archive, executor = HistoryMemory(), FakeExecutor()
        for value in (sample('/namer-unknown\nA', None), sample('/namer-peeks', '/namer-peeks')):
            self.assertEqual(recompute(value, archive, executor, {}), "failed")
        self.assertFalse(executor.bodies)
        self.assertFalse(archive.calls)
        self.assertEqual(next(iter(archive.jobs.values()))["result"]["errors"][0]["code"], "invalid_missing_newline")
        malformed = sample('/namer-pf\nA+ol:{bad\n B ', '/namer-pf')
        self.assertEqual(recompute(malformed, archive, executor, {}), "failed")
        lines = next(iter(archive.jobs.values()))["input"]["source_lines"]
        self.assertEqual(lines[0]["syntax"], {"kind": "unparsed", "value": "A+ol:{bad"})
        self.assertEqual(lines[1]["syntax"]["identity_parts"], [" B "])
        self.assertFalse(executor.bodies)

    def test_lost_lock_stops_before_new_calculation_and_old_writer_cannot_claim_other_job(self):
        archive, gate = Mock(), Mock()
        identity = uuid4()
        writer = HistoryWriter(archive, identity, uuid4(), gate)
        gate.execute.side_effect = OSError("secret-not-propagated")
        with self.assertRaisesRegex(ArchiveError, "history_lock_lost"):
            writer.begin_call(identity, "md5", "any", {})
        archive.begin_call.assert_not_called()
        gate.execute.side_effect = None
        with self.assertRaisesRegex(ArchiveError, "wrong_job"):
            writer.finish_job(uuid4(), "succeeded", {})
        archive.finish_job.assert_not_called()

    def test_offline_local_execution_uses_player_data_without_dispatch_or_client(self):
        archive = HistoryMemory()
        previous_rounds = namerena.TSWN_COMPARE_ROUNDS
        previous_compare = namerena.USE_TSWN_COMPARE
        self.addCleanup(setattr, namerena, "TSWN_COMPARE_ROUNDS", previous_rounds)
        self.addCleanup(setattr, namerena, "USE_TSWN_COMPARE", previous_compare)
        executor = EngineExecutor(namerena, Settings(rounds=1, pair_rounds=1))
        executor.verify = lambda: None
        original = namerena.dispatch_msg
        namerena.dispatch_msg = Mock(side_effect=AssertionError("不允许进入聊天路径"))
        try:
            self.assertEqual(recompute(sample(), archive, executor, {}), "succeeded")
        finally:
            namerena.dispatch_msg = original
        output = next(iter(archive.calls.values()))["output"]["items"][0]
        self.assertEqual(output["name"], ' A+B[" ')
        self.assertEqual(output["team"], ' A+B[" ')
