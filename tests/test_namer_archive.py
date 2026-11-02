"""保护全链路结构化、失败不冒充成功，以及并发/后台归属。"""
import json
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from plugins import namerena


class MemoryArchive:
    def __init__(self):
        self.jobs = {}
        self.calls = {}
        self.lock = threading.Lock()
        self.events = {}

    def begin_job(self, source, payload, version):
        identity = uuid4()
        with self.lock:
            self.jobs[identity] = {"source": source, "input": payload, "state": "started", "deliveries": []}
            self.events[identity] = threading.Event()
        return identity

    def begin_call(self, job, engine, operation, payload):
        identity = uuid4()
        self.calls[identity] = {"job": job, "engine": engine, "operation": operation, "input": payload, "state": "started"}
        return identity

    def finish_call(self, identity, output, error, exit_code, elapsed):
        self.calls[identity].update(output=output, error=error, state="failed" if error else "succeeded", exit_code=exit_code)

    def finish_job(self, job, state, result):
        self.jobs[job].update(state=state, result=result)
        self.events[job].set()

    def delivery(self, job, event):
        self.jobs[job]["deliveries"].append(event)


def message(text, identity="message-test"):
    return SimpleNamespace(content=text, id=identity, sender_id="sender-test", room_id="room-test",
                           is_reply=False, is_from_self=False, reply_with=lambda value: value)


def client(account="bot-test"):
    value = Mock()
    value.status.self_id = account
    value.send_message.return_value = True
    return value


class NamerArchiveTests(unittest.TestCase):
    def setUp(self):
        self.archive = MemoryArchive()
        self.scope = patch.multiple(namerena, _ARCHIVE=self.archive, _ARCHIVE_ERROR=None)
        self.scope.start()
        namerena._shutdown.clear()
        self.addCleanup(self.scope.stop)

    def test_pf_records_actual_outputs_and_aligned_partial_result(self):
        """同名输入按序号分开；单次引擎失败不抹掉其它项，也不保存回复正文。"""
        count = 0

        def run(command, **kwargs):
            nonlocal count
            if "--json" in command:
                count += 1
                if count == 6:
                    return subprocess.CompletedProcess(command, 0, "{malformed-json", "private diagnostic")
                output = {"version": 1, "kind": "score", "score_accumulated": count,
                          "rounds_completed": 100, "rounds_requested": 10}
                return subprocess.CompletedProcess(command, 0, json.dumps(output), "")
            return subprocess.CompletedProcess(command, 0, "pp|pd|qp|qd\n100|200|300|400\n500|600|700|800", "")

        with patch.object(namerena, "resolve_tswn_runner", return_value=(["tswn-cli"], None)),              patch.object(namerena, "get_js_runtime", return_value="node"),              patch.object(namerena, "get_runtime_version", return_value="test-runtime"),              patch.object(namerena, "get_tswn_version", return_value="test-engine"),              patch.object(namerena.subprocess, "run", side_effect=run):
            namerena.dispatch_msg(message("/namer-pf\nA\nA"), client())
        job = next(iter(self.archive.jobs.values()))
        self.assertEqual(job["state"], "partial")
        first, second = job["result"]["items"]
        self.assertEqual(first["engines"]["md5"]["scores"]["pd"], 200)
        self.assertEqual(second["engines"]["md5"]["state"], "partial")
        self.assertEqual(second["engines"]["tswn"]["scores"]["pp"], 500)
        self.assertEqual(first["comparison"]["pp"]["absolute_difference"], 0)
        stored = json.dumps({"jobs": list(self.archive.jobs.values()), "calls": list(self.archive.calls.values())}, default=str)
        self.assertNotIn("private diagnostic", stored)
        self.assertNotIn("开始计算", stored)
        self.assertNotIn("/namer-pf\n", stored)

    def test_local_names_match_computed_identity_and_invalid_input_is_failed(self):
        """普通名字中的 +、引号、括号、标记及空白不能被 DSL 解析改写。"""
        namerena.dispatch_msg(message('/namer-peek\n A+B[" \n!test!\n!'), client())
        job = next(iter(self.archive.jobs.values()))
        actual = next(iter(self.archive.calls.values()))["output"]["items"]
        for source, output in zip(job["input"]["groups"], actual):
            self.assertEqual(source["members"][0]["name"], output["name"])
            self.assertEqual(source["members"][0]["team"], output["team"])
        self.assertEqual(job["state"], "succeeded")
        namerena.dispatch_msg(message('/namer-base', 'invalid-message'), client())
        invalid = list(self.archive.jobs.values())[-1]
        self.assertEqual(invalid["state"], "failed")
        self.assertEqual(list(self.archive.calls.values())[-1]["error"]["code"], "invalid_input")

    def test_concurrent_background_requests_keep_account_and_job_association(self):
        """后台计算不能提前完成 job，也不能串到另一个请求的 ContextVar。"""
        barrier = threading.Barrier(2)
        release = threading.Event()

        def calculate(groups):
            barrier.wait(timeout=5)
            self.assertTrue(release.wait(5))
            session = namerena.CURRENT.get()
            handle = session.begin("tswn", "cqp", {"groups": groups})
            session.end(handle, {"version": 1, "kind": "cqp", "items": [{"input_index": 0, "score": 42}]}, exit_code=0)
            return "42 test"

        with patch.object(namerena, "run_tswn_cqp", side_effect=calculate):
            namerena.dispatch_msg(message('/namer-cqp\nA', 'first'), client('bot-a'))
            namerena.dispatch_msg(message('/namer-cqp\nB', 'second'), client('bot-b'))
            self.assertTrue(all(job["state"] == "started" for job in self.archive.jobs.values()))
            release.set()
            for event in self.archive.events.values():
                self.assertTrue(event.wait(5))
        for call in self.archive.calls.values():
            owner = self.archive.jobs[call["job"]]
            expected = 'A' if owner["source"]["account"] == 'bot-a' else 'B'
            self.assertEqual(call["input"]["groups"], [expected])
            self.assertEqual(owner["state"], "succeeded")
