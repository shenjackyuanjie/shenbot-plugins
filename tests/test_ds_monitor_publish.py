"""用可控工作队列和本地 HTTP 服务验证发布事务，不运行部署或真实通知。"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from plugins import ds_monitor as monitor


class QueuedWorkers:
    """把线程启动与执行分开，确定性复现连续命令和发布中到达的变化。"""

    def __init__(self):
        self.jobs = []

    def thread(self, target, args=(), kwargs=None, **_options):
        return Mock(start=lambda: self.jobs.append((target, args, kwargs or {})))

    def run_next(self):
        target, args, kwargs = self.jobs.pop(0)
        return target(*args, **kwargs)


class PublishFlowTests(unittest.TestCase):
    def setUp(self):
        self.state = patch.multiple(
            monitor,
            _publish_enabled=True,
            _publish_in_progress=False,
            _publish_pending=False,
            _publish_failures=0,
            _last_publish_at=0.0,
            _last_publish_ok=None,
            _last_publish_cmd_at=0.0,
            _update_in_progress=False,
            _unloading=False,
        )
        self.state.start()
        self.addCleanup(self.state.stop)
        self.logs = patch.object(monitor, "log")
        self.logs.start()
        self.addCleanup(self.logs.stop)

    def test_reservation_coalesces_changes_and_notification_failure_never_redeploys(self):
        workers = QueuedWorkers()
        result = Mock(returncode=0, stdout="Deployment complete!", stderr="")
        with (
            patch.object(monitor.threading, "Thread", side_effect=workers.thread),
            patch.object(monitor.os.path, "isfile", return_value=True),
            patch.object(monitor, "repo_root", return_value="unused-test-root"),
            patch.object(monitor, "archive_entries", return_value=[]),
            patch.object(monitor, "site_stats", return_value=(7, 1.5)),
            patch.object(monitor.subprocess, "run", return_value=result) as deploy,
            patch.object(monitor, "noticer_post", return_value=False) as notify,
        ):
            monitor.maybe_publish(1)
            self.assertTrue(monitor._publish_in_progress)
            monitor.maybe_publish(2)
            self.assertEqual(len(workers.jobs), 1)
            # 直接手动调用同样要经过占位，不能并发跑第二个 deploy。
            monitor.publish_once(False)
            self.assertEqual(deploy.call_count, 0)
            self.assertFalse(monitor.reserve_publish(automatic=False))
            workers.run_next()
            self.assertEqual(deploy.call_count, 1)
            self.assertEqual(len(workers.jobs), 1)
            self.assertTrue(monitor._publish_in_progress)
            workers.run_next()
            self.assertEqual(deploy.call_count, 2)
            self.assertEqual(notify.call_count, 2)
            self.assertFalse(workers.jobs)
            self.assertFalse(monitor._publish_in_progress)
            self.assertTrue(monitor._last_publish_ok)
            self.assertGreater(monitor._last_publish_at, 1_700_000_000)
            monitor._update_in_progress = True
            monitor.maybe_publish(1)
            self.assertFalse(workers.jobs)
            self.assertTrue(monitor._publish_pending)
            monitor._update_in_progress = False
            monitor._unloading = True
            monitor.maybe_publish(1)
            self.assertFalse(workers.jobs)

    def test_missing_script_counts_toward_circuit_and_manual_success_recovers_it(self):
        workers = QueuedWorkers()
        msg, client = Mock(), Mock()
        with (
            patch.object(monitor.threading, "Thread", side_effect=workers.thread),
            patch.object(monitor, "repo_root", return_value="unused-test-root"),
            patch.object(monitor.os.path, "isfile", return_value=False) as exists,
            patch.object(monitor, "noticer_post", return_value=True),
            patch.object(monitor, "is_admin", return_value=True),
            patch.object(monitor, "archive_entries", return_value=[]),
            patch.object(monitor, "site_stats", return_value=(1, 0.1)),
            patch.object(monitor.subprocess, "run") as deploy,
        ):
            for _ in range(monitor.PUBLISH_MAX_FAILURES):
                monitor.maybe_publish(1)
                workers.run_next()
            deploy.assert_not_called()
            self.assertTrue(monitor._publish_pending)
            monitor.maybe_publish(10)
            monitor.maybe_publish(0)
            self.assertFalse(workers.jobs)
            exists.return_value = True
            deploy.return_value = Mock(returncode=0, stdout="Deployment complete!", stderr="")
            monitor.cmd_deploy(msg, client)
            self.assertEqual(len(workers.jobs), 1)
            workers.run_next()
            self.assertEqual(monitor._publish_failures, 0)
            self.assertFalse(monitor._publish_pending)
            self.assertTrue(monitor._last_publish_ok)
            deploy.assert_called_once()
            monitor.maybe_publish(1)
            self.assertEqual(len(workers.jobs), 1)
            # 卸载前已预占、尚未执行的发布不能真正启动子进程。
            monitor._unloading = True
            workers.run_next()
            deploy.assert_called_once()
            self.assertFalse(monitor._publish_in_progress)

    def test_thread_and_reply_failures_release_reservations(self):
        worker = Mock()
        worker.start.side_effect = RuntimeError("线程无法启动")
        with (
            patch.object(monitor.threading, "Thread", return_value=worker),
            patch.object(monitor, "is_admin", return_value=True),
        ):
            monitor.maybe_publish(1)
            self.assertFalse(monitor._publish_in_progress)
            self.assertTrue(monitor._publish_pending)
            client = Mock()
            client.send_message.side_effect = RuntimeError("回复失败")
            monitor.cmd_deploy(Mock(), client)
            self.assertFalse(monitor._publish_in_progress)
            self.assertTrue(monitor._publish_pending)


class NoticerAcknowledgementTests(unittest.TestCase):
    def test_business_failures_and_partial_broadcast_are_not_full_success(self):
        received = []
        replies = [b'{"status":"error"}', b'{"status":"ok"}', b'not-json', b'{"status":"ok"}']

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((payload, self.headers.get("Authorization")))
                body = replies.pop(0)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cfg = {"noticer": {
            "url": f"http://127.0.0.1:{server.server_port}/v1/send",
            "room": "primary", "rooms": ["extra", "primary"], "token": "test-only",
        }}
        try:
            with (
                patch.object(monitor, "load_rust_config", return_value=cfg),
                patch.object(monitor, "log"),
            ):
                self.assertFalse(monitor.noticer_post("测试发布结论"))
                cfg["noticer"]["rooms"] = []
                self.assertFalse(monitor.noticer_post("非法确认正文"))
                self.assertTrue(monitor.noticer_post("确认成功"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual([item[0]["room"] for item in received], ["primary", "extra", "primary", "primary"])
        self.assertTrue(all(item[1] == "Bearer test-only" for item in received))
        self.assertTrue(all("test-only" not in item[0]["message"] for item in received))


if __name__ == "__main__":
    unittest.main()
