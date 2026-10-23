"""监测插件生命周期回归：用假进程和临时副本验证事务、取消与进程所有权。

不读取用户配置，不运行 cargo/watch/deploy，不调用模型或发送通知。
"""

import contextlib
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from threading import Thread as RealThread
from unittest.mock import Mock, patch

from plugins import ds_monitor as monitor


class FakeProcess:
    """可模拟正常退出或拒绝停止的进程，不创建操作系统子进程。"""

    def __init__(self, returncode=None, stuck=False):
        self.pid = 12345
        self.returncode = returncode
        self.stuck = stuck
        self.stdout = []

    def poll(self):
        return self.returncode

    def terminate(self):
        if not self.stuck:
            self.returncode = 0

    def kill(self):
        if not self.stuck:
            self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-watch", timeout)
        return self.returncode


class LifecycleTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, enabled=True):
        """将所有外部副作用换成 Mock，后台线程排队，由测试明确驱动执行顺序。"""
        with tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as stack:
            target = Path(root, "ds-monitor-copy.exe")
            source = Path(root, "ds-monitor.exe")
            target.write_bytes(b"old-binary")
            source.write_bytes(b"new-binary")
            jobs = []

            class QueuedThread:
                def __init__(self, target, args=(), daemon=None):
                    self.target, self.args = target, args

                def start(self):
                    jobs.append((self.target, self.args))

            stack.enter_context(patch.multiple(
                monitor, _binary_path=str(target), _config_path="", _enabled=enabled,
                _unloading=False, _lifecycle_generation=0, _watch_generation=0,
                _watch_exited_generation=-1, _watch_process=None, _watch_thread=None,
                _watch_stop_requested=False, _cycle_result_generation=-1,
                _cycle_event=threading.Event(), _last_update_cmd_at=0.0,
                _last_restart_cmd_at=0.0, _update_in_progress=False,
                _publish_in_progress=False, _watch_lock=threading.Lock(),
                _lifecycle_lock=threading.RLock(), _operation_lock=threading.RLock(),
            ))
            stack.enter_context(patch.object(monitor.threading, "Thread", QueuedThread))
            stack.enter_context(patch.object(monitor, "PLUGIN_MANIFEST", types.SimpleNamespace(version="offline")))
            stack.enter_context(patch.object(monitor, "validate_binary", return_value=True))
            stack.enter_context(patch.object(monitor, "repo_root", return_value=root))
            stack.enter_context(patch.object(monitor, "work_dir", return_value=root))
            stack.enter_context(patch.object(monitor, "is_admin", return_value=True))
            stack.enter_context(patch.object(monitor, "log"))
            stack.enter_context(patch.object(monitor, "notify_error"))
            stack.enter_context(patch.object(monitor, "wait_cycle_result", return_value=True))
            stack.enter_context(patch.object(monitor, "cycle_report", return_value="本轮正常"))
            build = stack.enter_context(patch.object(
                monitor.subprocess, "run", return_value=types.SimpleNamespace(
                    returncode=0, stdout="", stderr="")))
            popen = stack.enter_context(patch.object(
                monitor.subprocess, "Popen", side_effect=lambda *a, **kw: FakeProcess()))
            client = Mock()
            msg = Mock()
            msg.reply_with.side_effect = lambda text: text
            yield types.SimpleNamespace(
                target=target, source=source, jobs=jobs, build=build, popen=popen,
                client=client, msg=msg, stack=stack, root=Path(root),
            )

    def run_job(self, fx):
        target, args = fx.jobs.pop(0)
        target(*args)

    def report(self, fx):
        return "\n".join(str(call.args[0]) for call in fx.client.send_message.call_args_list)

    def test_update_transaction_keeps_old_watch_until_cutover_and_rolls_back(self):
        for mode in ("build_failure", "copy_failure", "replace_failure", "start_failure",
                     "cycle_exit", "cycle_eof", "rollback_stop_failure", "rollback_replace_failure",
                     "cycle_timeout", "success", "off"):
            with self.subTest(mode=mode), self.fixture(enabled=mode != "off") as fx:
                old = FakeProcess()
                if mode != "off":
                    monitor._watch_process = old

                def build(*args, **kwargs):
                    self.assertIsNone(old.poll(), "构建期间不可停掉旧 watch")
                    self.assertEqual(fx.target.read_bytes(), b"old-binary")
                    return types.SimpleNamespace(returncode=1 if mode == "build_failure" else 0,
                                                 stdout="", stderr="模拟构建失败")

                fx.build.side_effect = build
                if mode == "copy_failure":
                    def broken_copy(source, destination):
                        Path(destination).write_bytes(b"partial")
                        raise OSError("模拟复制中断")
                    fx.stack.enter_context(patch.object(monitor.shutil, "copy2", side_effect=broken_copy))
                elif mode == "replace_failure":
                    fx.stack.enter_context(patch.object(monitor.os, "replace", side_effect=OSError("模拟替换失败")))
                if mode == "start_failure":
                    fx.popen.side_effect = [FakeProcess(returncode=1), FakeProcess()]
                if mode == "rollback_stop_failure":
                    fx.popen.side_effect = [FakeProcess(stuck=True)]
                if mode == "rollback_replace_failure":
                    replace = monitor.os.replace
                    calls = []
                    def fail_rollback(source, target):
                        calls.append((source, target))
                        if len(calls) == 2:
                            raise OSError("模拟回滚替换失败")
                        return replace(source, target)
                    fx.stack.enter_context(patch.object(monitor.os, "replace", side_effect=fail_rollback))
                if mode in ("cycle_exit", "cycle_eof", "rollback_stop_failure", "rollback_replace_failure", "cycle_timeout"):
                    def first_cycle(generation, timeout):
                        if mode == "cycle_exit":
                            monitor._watch_process.returncode = 1
                        if mode != "cycle_timeout":
                            monitor._watch_exited_generation = generation
                        return False
                    monitor.wait_cycle_result.side_effect = first_cycle
                monitor.cmd_update(fx.msg, fx.client)
                self.assertTrue(monitor._update_in_progress)
                self.run_job(fx)
                self.assertFalse(monitor._update_in_progress)
                expected = b"new-binary" if mode in ("success", "off", "rollback_stop_failure", "rollback_replace_failure", "cycle_timeout") else b"old-binary"
                self.assertEqual(fx.target.read_bytes(), expected)
                if mode in ("build_failure", "copy_failure"):
                    self.assertIs(monitor._watch_process, old)
                    self.assertIsNone(old.poll())
                    fx.popen.assert_not_called()
                elif mode == "off":
                    self.assertFalse(monitor._enabled)
                    fx.popen.assert_not_called()
                    self.assertIn("保持暂停", self.report(fx))
                elif mode == "rollback_replace_failure":
                    self.assertIsNone(monitor._watch_process)
                    self.assertIn("回滚失败", self.report(fx))
                else:
                    self.assertIsNotNone(monitor._watch_process)
                    self.assertIsNone(monitor._watch_process.poll())
                if mode in ("start_failure", "cycle_exit", "cycle_eof"):
                    self.assertIn("回滚", self.report(fx))
                leftovers = list(fx.root.glob(".ds-monitor-update-*"))
                if mode in ("rollback_stop_failure", "rollback_replace_failure"):
                    self.assertEqual(len(leftovers), 1)
                    self.assertEqual((leftovers[0] / "old.exe").read_bytes(), b"old-binary")
                    if mode == "rollback_stop_failure":
                        self.assertIn("禁止回滚覆盖", self.report(fx))
                else:
                    self.assertFalse(leftovers)

    def test_off_unload_and_update_reservation_cancel_queued_starts(self):
        for command in ("check", "on", "update"):
            for cancel in ("off", "unload"):
                with self.subTest(command=command, cancel=cancel), self.fixture() as fx:
                    if command == "check":
                        monitor.cmd_check(fx.msg, fx.client)
                    elif command == "on":
                        monitor.cmd_enable(fx.msg, fx.client, True)
                    else:
                        monitor.cmd_update(fx.msg, fx.client)
                    if cancel == "off":
                        monitor.cmd_enable(fx.msg, fx.client, False)
                    else:
                        monitor.on_unload()
                    self.run_job(fx)
                    fx.popen.assert_not_called()
                    fx.build.assert_not_called()
                    self.assertIsNone(monitor._watch_process)
                    self.assertFalse(monitor._update_in_progress)
        for cancel in ("off", "unload"):
            with self.subTest(during_build=cancel), self.fixture() as fx:
                old = FakeProcess()
                monitor._watch_process = old

                def build(*args, **kwargs):
                    count = len(fx.jobs)
                    monitor.cmd_check(fx.msg, fx.client)
                    monitor.cmd_enable(fx.msg, fx.client, True)
                    self.assertEqual(len(fx.jobs), count, "更新占位期间不能排入新启动")
                    if cancel == "off":
                        monitor.cmd_enable(fx.msg, fx.client, False)
                    else:
                        monitor.on_unload()
                    return types.SimpleNamespace(returncode=0)

                fx.build.side_effect = build
                monitor.cmd_update(fx.msg, fx.client)
                self.run_job(fx)
                fx.popen.assert_not_called()
                self.assertEqual(fx.target.read_bytes(), b"old-binary")
                self.assertNotIn("恢复监测", self.report(fx))

    def test_stop_failure_preserves_handle_and_eof_wakes_waiters(self):
        wait_result = monitor.wait_cycle_result
        with self.fixture() as fx:
            old = FakeProcess(stuck=True)
            monitor._watch_process = old
            self.assertFalse(monitor.stop_watch(timeout=0))
            self.assertIs(monitor._watch_process, old)
            with patch.object(monitor.os, "replace") as replace:
                monitor.cmd_update(fx.msg, fx.client)
                self.run_job(fx)
                replace.assert_not_called()
            self.assertIs(monitor._watch_process, old)
            monitor._enabled = False
            with patch.object(monitor, "binary_version", return_value="offline-version"):
                monitor.cmd_status(fx.msg, fx.client)
            self.assertIn("进程尚未退出", self.report(fx))
            monitor._enabled = True
            # stop 持生命周期锁时，输出线程的 finally 仍必须能独立收尾。
            with monitor._lifecycle_lock:
                reader = RealThread(target=monitor.watch_stdout_loop, args=(old, monitor._watch_generation), daemon=True)
                reader.start()
                reader.join(timeout=1)
                self.assertFalse(reader.is_alive(), "stdout finally 不能等待生命周期锁")
            self.assertIs(monitor._watch_process, old, "EOF 不代表进程已经退出")
            self.assertFalse(wait_result(monitor._watch_generation, 30))
            self.assertTrue(monitor._cycle_event.is_set())
        with self.fixture() as fx:
            fx.popen.side_effect = [FakeProcess(returncode=1)]
            self.assertFalse(monitor.start_watch())
            self.assertIsNone(monitor._watch_process)
            self.assertFalse(fx.jobs)
            with patch.object(monitor, "binary_version", return_value="offline-version"):
                monitor.cmd_status(fx.msg, fx.client)
            self.assertIn("已开启但进程未运行", self.report(fx))

    def test_update_reservation_is_released_if_reply_or_thread_start_fails(self):
        for fail in ("reply", "thread"):
            with self.subTest(fail=fail), self.fixture() as fx:
                if fail == "reply":
                    fx.client.send_message.side_effect = RuntimeError("模拟回复失败")
                else:
                    fx.stack.enter_context(patch.object(
                        monitor.threading.Thread, "start", side_effect=RuntimeError("模拟线程失败")))
                monitor.cmd_update(fx.msg, fx.client)
                if fail == "reply":
                    self.assertTrue(monitor._update_in_progress, "回复失败不能撤销已受理任务")
                    self.run_job(fx)
                    fx.build.assert_called_once()
                    self.assertEqual(fx.target.read_bytes(), b"new-binary")
                else:
                    fx.build.assert_not_called()
                    fx.popen.assert_not_called()
                self.assertFalse(monitor._update_in_progress)
        with self.fixture() as fx:
            monitor._publish_in_progress = True
            monitor.cmd_update(fx.msg, fx.client)
            self.assertFalse(monitor._update_in_progress)
            self.assertFalse(fx.jobs)


if __name__ == "__main__":
    unittest.main()
