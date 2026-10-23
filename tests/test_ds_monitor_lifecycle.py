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

            stack.enter_context(
                patch.multiple(
                    monitor,
                    _binary_path=str(target),
                    _config_path="",
                    _enabled=enabled,
                    _unloading=False,
                    _lifecycle_generation=0,
                    _watch_generation=0,
                    _watch_exited_generation=-1,
                    _watch_process=None,
                    _watch_thread=None,
                    _watch_stop_requested=False,
                    _cycle_result_generation=-1,
                    _cycle_event=threading.Event(),
                    _last_update_cmd_at=0.0,
                    _last_restart_cmd_at=0.0,
                    _update_in_progress=False,
                    _publish_in_progress=False,
                    _watch_lock=threading.Lock(),
                    _lifecycle_lock=threading.RLock(),
                    _operation_lock=threading.RLock(),
                )
            )
            stack.enter_context(patch.object(monitor.threading, "Thread", QueuedThread))
            stack.enter_context(
                patch.object(
                    monitor, "PLUGIN_MANIFEST", types.SimpleNamespace(version="offline")
                )
            )
            stack.enter_context(
                patch.object(monitor, "validate_binary", return_value=True)
            )
            stack.enter_context(patch.object(monitor, "repo_root", return_value=root))
            stack.enter_context(patch.object(monitor, "work_dir", return_value=root))
            stack.enter_context(patch.object(monitor, "is_admin", return_value=True))
            stack.enter_context(patch.object(monitor, "log"))
            stack.enter_context(patch.object(monitor, "notify_error"))
            stack.enter_context(
                patch.object(monitor, "wait_cycle_result", return_value=True)
            )
            stack.enter_context(
                patch.object(monitor, "cycle_report", return_value="本轮正常")
            )
            build = stack.enter_context(
                patch.object(
                    monitor.subprocess,
                    "run",
                    return_value=types.SimpleNamespace(
                        returncode=0, stdout="", stderr=""
                    ),
                )
            )
            popen = stack.enter_context(
                patch.object(
                    monitor.subprocess,
                    "Popen",
                    side_effect=lambda *a, **kw: FakeProcess(),
                )
            )
            client = Mock()
            msg = Mock()
            msg.reply_with.side_effect = lambda text: text
            yield types.SimpleNamespace(
                target=target,
                source=source,
                jobs=jobs,
                build=build,
                popen=popen,
                client=client,
                msg=msg,
                stack=stack,
                root=Path(root),
            )

    def run_job(self, fx):
        target, args = fx.jobs.pop(0)
        target(*args)

    def report(self, fx):
        return "\n".join(
            str(call.args[0]) for call in fx.client.send_message.call_args_list
        )

    def test_update_transaction_keeps_old_watch_until_cutover_and_rolls_back(self):
        # 只覆盖构建失败、替换后启动失败、成功三个决定能否安全切换的阶段。
        for mode in ("build_failure", "start_failure", "success"):
            with self.subTest(mode=mode), self.fixture() as fx:
                old = FakeProcess()
                monitor._watch_process = old

                def build(*args, **kwargs):
                    self.assertIsNone(old.poll(), "构建期间不能停旧进程")
                    self.assertEqual(fx.target.read_bytes(), b"old-binary")
                    return types.SimpleNamespace(
                        returncode=1 if mode == "build_failure" else 0
                    )

                fx.build.side_effect = build
                if mode == "start_failure":
                    fx.popen.side_effect = [FakeProcess(returncode=1), FakeProcess()]
                monitor.cmd_update(fx.msg, fx.client)
                self.run_job(fx)
                self.assertFalse(monitor._update_in_progress)
                self.assertEqual(
                    fx.target.read_bytes(),
                    b"new-binary" if mode == "success" else b"old-binary",
                )
                if mode == "build_failure":
                    self.assertIs(monitor._watch_process, old)
                    self.assertIsNone(old.poll())
                    fx.popen.assert_not_called()
                else:
                    self.assertIsNotNone(monitor._watch_process)
                    self.assertIsNone(monitor._watch_process.poll())
                if mode == "start_failure":
                    self.assertIn("回滚", self.report(fx))
                self.assertFalse(list(fx.root.glob(".ds-monitor-update-*")))

    def test_off_unload_and_update_reservation_cancel_queued_starts(self):
        # 排队的更新必须被卸载取消，不能在关闭后才启动构建或进程。
        with self.fixture() as fx:
            monitor.cmd_update(fx.msg, fx.client)
            monitor.on_unload()
            self.run_job(fx)
            fx.build.assert_not_called()
            fx.popen.assert_not_called()
            self.assertFalse(monitor._update_in_progress)
        # 正在构建时收到暂停，不应切换新副本或重新开启监测。
        with self.fixture() as fx:
            monitor._watch_process = FakeProcess()

            def build(*args, **kwargs):
                monitor.cmd_enable(fx.msg, fx.client, False)
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
            with patch.object(
                monitor, "binary_version", return_value="offline-version"
            ):
                monitor.cmd_status(fx.msg, fx.client)
            self.assertIn("进程尚未退出", self.report(fx))
            monitor._enabled = True
            # stop 持生命周期锁时，输出线程的 finally 仍必须能独立收尾。
            with monitor._lifecycle_lock:
                reader = RealThread(
                    target=monitor.watch_stdout_loop,
                    args=(old, monitor._watch_generation),
                    daemon=True,
                )
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
            with patch.object(
                monitor, "binary_version", return_value="offline-version"
            ):
                monitor.cmd_status(fx.msg, fx.client)
            self.assertIn("已开启但进程未运行", self.report(fx))


if __name__ == "__main__":
    unittest.main()
