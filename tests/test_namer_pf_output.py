"""真实 PF 指标协议与逐非空行输入对齐的必要回归。"""
import subprocess
import unittest
from unittest.mock import Mock

from plugins import namerena

# isort: split
# 插件先建立 support 导入根，避免排序反转后独立运行失败。
from test_namer_archive import MemoryArchive


class PfOutputTests(unittest.TestCase):
    def test_native_metric_cycles_preserve_duplicate_inputs_and_blank_lines_are_not_teams(self):
        body = "A\nA\n\nB"
        output = ("A pp:10000\nA pd:0\nA qp:0\nA qd:0\nA sum:10000\n"
                  "A pp:0\nA pd:10000\nA qp:0\nA qd:0\nA sum:10000\n"
                  "B pp:0\nB pd:0\nB qp:10000\nB qd:0\nB sum:10000\n")
        archive = MemoryArchive()
        payload = namerena.parse_input("/namer-pf\n" + body, "/namer-pf", 1)
        identity = archive.begin_job({}, payload, "synthetic")
        session = namerena.Session(archive, identity, payload)
        token = namerena.CURRENT.set(session)
        command = ["synthetic-cli", "namer-pf", "-n", "1"]
        run = Mock(return_value=subprocess.CompletedProcess(command, 0, output, ""))
        try:
            result = namerena.process(run, command, engine_version="synthetic", input=body)
            self.assertEqual(result.returncode, 0)
            call = next(iter(archive.calls.values()))
            self.assertEqual(call["state"], "succeeded")
            self.assertEqual([item["input_index"] for item in call["input"]["groups"]], [0, 1, 2])
            items = session.summary()["items"]
            self.assertEqual([item["engines"]["tswn"]["scores"]["pp"] for item in items], [10000, 0, 0])
            self.assertEqual(items[1]["engines"]["tswn"]["scores"]["pd"], 10000)
            self.assertEqual(items[2]["engines"]["tswn"]["scores"]["qp"], 10000)
            self.assertTrue(all(item["engines"]["tswn"]["scores"]["sum"] == 10000 for item in items))
            self.assertNotIn("A pp:", str(call["output"]))
            # 子进程退出0也不能把残缺周期当成功，更不能继承上一次成功输出。
            run.return_value = subprocess.CompletedProcess(command, 0, output.removesuffix("B sum:10000\n"), "")
            result = namerena.process(run, command, engine_version="synthetic", input=body)
            failed = list(archive.calls.values())[-1]
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["error"]["code"], "parse_failed")
            self.assertIsNone(failed["output"])
        finally:
            try:
                session.release()
            finally:
                namerena.CURRENT.reset(token)
