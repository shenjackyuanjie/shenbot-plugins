from __future__ import annotations

import builtins
import http.client
import importlib.util
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from typing import Any


PLUGIN_PATH = Path(__file__).parents[1] / "plugins" / "noticer.py"


class _ConfigStorage:
    def __init__(self, **values: object) -> None:
        self.values = values

    def get_value(self, name: str) -> object:
        return self.values.get(name)


class _PluginManifest:
    def __init__(
        self,
        *,
        plugin_id: str,
        name: str,
        version: str,
        description: str,
        authors: list[str],
        config: dict[str, _ConfigStorage],
    ) -> None:
        del plugin_id, name, description, authors
        self.version = version
        self.config_values = config

    def config_unchecked(self, name: str) -> _ConfigStorage:
        return self.config_values[name]


def _load_noticer() -> types.ModuleType:
    api = types.ModuleType("shenbot_api")
    api.ConfigStorage = _ConfigStorage
    api.PluginManifest = _PluginManifest
    api.python_config_path = lambda: str(PLUGIN_PATH.parent)
    sys.modules["shenbot_api"] = api

    module_name = "_noticer_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _SendingMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.image: tuple[bytes, str, bool] | None = None

    def set_img(self, data: bytes, image_type: str, as_sticker: bool) -> None:
        self.image = (data, image_type, as_sticker)


class _Room:
    def __init__(self, room_id: int) -> None:
        self.room_id = room_id

    def new_message_to(self, content: str) -> _SendingMessage:
        return _SendingMessage(content)


class _Status:
    def __init__(self, rooms: list[_Room]) -> None:
        self.rooms = rooms


class _Client:
    def __init__(
        self,
        rooms: list[_Room],
        *,
        gate: threading.Event | None = None,
    ) -> None:
        self.client_id = 7
        self.status = _Status(rooms)
        self.sent: list[_SendingMessage] = []
        self.gate = gate

    def send_message(self, message: _SendingMessage) -> bool:
        if self.gate is not None:
            self.gate.wait(timeout=2)
        self.sent.append(message)
        return True

    def info(self, message: str) -> None:
        del message

    def warn(self, message: str) -> None:
        del message


class _IncomingMessage:
    content = ""
    is_from_self = False
    is_reply = False


class NoticerHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.noticer = _load_noticer()
        self.noticer._rooms = {"notice": -100, "bot": -200}
        self.noticer._room_descriptions = {
            "notice": "notice room",
            "bot": "bot room",
        }
        self.noticer._auth_token = ""
        self.noticer._direct_token = ""
        self.noticer._queue_capacity = 8
        self.noticer._send_timeout_seconds = 2
        self.client = _Client([_Room(-100), _Room(-200), _Room(-300)])
        self.noticer._ica_client = self.client
        self.noticer._set_persisted_client(self.client)
        self.noticer._start_send_worker()
        self.server = self.noticer._NoticerServer(
            ("127.0.0.1", 0),
            self.noticer._NoticerHandler,
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.server_thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.noticer._stop_send_worker()
        with self.noticer._idempotency_lock:
            self.noticer._idempotency_entries.clear()
        if hasattr(builtins, self.noticer._RUNTIME_STATE_KEY):
            delattr(builtins, self.noticer._RUNTIME_STATE_KEY)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=3,
        )
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result_headers = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        return response.status, result_headers, json.loads(payload)

    @staticmethod
    def json_body(data: dict[str, object]) -> bytes:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def post(
        self,
        path: str,
        data: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        return self.request(
            "POST",
            path,
            self.json_body(data),
            request_headers,
        )

    def test_legacy_room_response_stays_compatible(self) -> None:
        status, _, body = self.post(
            "/send",
            {"room": "notice", "message": "hello"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "room": "notice"})
        self.assertEqual(self.client.sent[0].content, "hello")

    def test_legacy_room_id_is_strict_and_deprecated(self) -> None:
        status, headers, body = self.post(
            "/send",
            {"room_id": -300, "message": "legacy"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "room": "room_-300"})
        self.assertEqual(headers["deprecation"], "true")

        for value in (True, 1.5):
            status, _, body = self.post(
                "/send",
                {"room_id": value, "message": "invalid"},
            )
            self.assertEqual(status, 400)
            self.assertIn("integer", body["error"])

    def test_non_standard_json_returns_400_and_server_survives(self) -> None:
        status, _, body = self.request(
            "POST",
            "/send",
            b'{"room_id":NaN,"message":"invalid"}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid JSON", body["error"])
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "running")

    def test_content_type_and_method_are_normalized(self) -> None:
        status, _, body = self.request(
            "POST",
            "/send",
            b'{"room":"notice","message":"x"}',
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertIn("Content-Type", body["error"])

        status, headers, body = self.request("PUT", "/health")
        self.assertEqual(status, 405)
        self.assertNotIn("server", headers)
        self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_optional_auth_and_direct_token(self) -> None:
        self.noticer._auth_token = "normal-secret"
        status, _, body = self.post(
            "/v1/send",
            {"room": "notice", "message": "x"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

        status, _, body = self.post(
            "/v1/send",
            {"room": "notice", "message": "x"},
            {"Authorization": "Bearer normal-secret"},
        )
        self.assertEqual(status, 200)

        status, _, body = self.post(
            "/v1/send/direct",
            {"room_id": -300, "message": "x"},
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "direct_api_disabled")

        self.noticer._direct_token = "direct-secret"
        status, _, _ = self.post(
            "/v1/send/direct",
            {"room_id": -300, "message": "x"},
        )
        self.assertEqual(status, 401)
        status, _, body = self.post(
            "/v1/send/direct",
            {"room_id": -300, "message": "x"},
            {"Authorization": "Bearer direct-secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["room_id"], -300)
        self.assertIn("request_id", body)

    def test_v1_strict_route_rejects_room_id(self) -> None:
        status, _, body = self.post(
            "/v1/send",
            {"room_id": -100, "message": "x"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "unknown_field")

    def test_v1_idempotency_reuses_result_and_rejects_conflict(self) -> None:
        headers = {"Idempotency-Key": "build-123"}
        first = self.post(
            "/v1/send",
            {"room": "notice", "message": "same"},
            headers,
        )
        second = self.post(
            "/v1/send",
            {"room": "notice", "message": "same"},
            headers,
        )
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(first[2]["request_id"], second[2]["request_id"])
        self.assertEqual(len(self.client.sent), 1)

        status, _, body = self.post(
            "/v1/send",
            {"room": "notice", "message": "different"},
            headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "idempotency_conflict")

    def test_image_mime_mismatch_is_rejected(self) -> None:
        import base64

        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
        status, _, body = self.post(
            "/v1/send",
            {
                "room": "notice",
                "image": {
                    "base64": base64.b64encode(png).decode("ascii"),
                    "type": "image/jpeg",
                },
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_image")

    def test_queue_is_bounded(self) -> None:
        self.noticer._stop_send_worker()
        self.noticer._queue_capacity = 1
        gate = threading.Event()
        self.client.gate = gate
        self.noticer._start_send_worker()

        jobs = [
            self.noticer._SendJob(
                request_id=str(index),
                room_name="notice",
                room_id=-100,
                message=str(index),
                image_bytes=None,
                image_type="image/png",
                as_sticker=False,
            )
            for index in range(3)
        ]
        self.assertTrue(self.noticer._enqueue_job(jobs[0])[0])
        for _ in range(100):
            with jobs[0].lock:
                if jobs[0].started:
                    break
            threading.Event().wait(0.005)
        self.assertTrue(jobs[0].started)
        self.assertTrue(self.noticer._enqueue_job(jobs[1])[0])
        accepted, detail = self.noticer._enqueue_job(jobs[2])
        self.assertFalse(accepted)
        self.assertEqual(detail, "send queue is full")
        gate.set()
        self.assertTrue(jobs[0].done.wait(timeout=2))
        self.assertTrue(jobs[1].done.wait(timeout=2))

    def test_queued_timeout_cancels_unsent_job(self) -> None:
        self.noticer._stop_send_worker()
        self.noticer._queue_capacity = 2
        self.noticer._send_timeout_seconds = 0.05
        gate = threading.Event()
        self.client.gate = gate
        self.noticer._start_send_worker()
        first = self.noticer._SendJob(
            request_id="first",
            room_name="notice",
            room_id=-100,
            message="first",
            image_bytes=None,
            image_type="image/png",
            as_sticker=False,
        )
        second = self.noticer._SendJob(
            request_id="second",
            room_name="notice",
            room_id=-100,
            message="second",
            image_bytes=None,
            image_type="image/png",
            as_sticker=False,
        )
        self.assertTrue(self.noticer._enqueue_job(first)[0])
        for _ in range(100):
            with first.lock:
                if first.started:
                    break
            threading.Event().wait(0.005)
        self.assertTrue(first.started)
        self.assertTrue(self.noticer._enqueue_job(second)[0])
        status, detail = self.noticer._wait_for_job(second)
        self.assertEqual(status, 504)
        self.assertIn("was not sent", detail)
        gate.set()
        self.assertTrue(first.done.wait(timeout=2))
        self.assertTrue(second.done.wait(timeout=2))
        self.assertEqual([message.content for message in self.client.sent], ["first"])

    def test_new_wrapper_with_same_client_id_does_not_repeat_capture_log(self) -> None:
        logs: list[str] = []
        self.noticer._log_info = logs.append
        replacement = _Client([_Room(-100)])
        replacement.client_id = self.client.client_id
        self.noticer.on_ica_message(_IncomingMessage(), replacement)
        self.assertIs(self.noticer._ica_client, replacement)
        self.assertNotIn("Client captured, webhook ready", logs)

        reconnected = _Client([_Room(-100)])
        reconnected.client_id = self.client.client_id + 1
        self.noticer.on_ica_message(_IncomingMessage(), reconnected)
        self.assertEqual(logs.count("Client captured, webhook ready"), 1)


if __name__ == "__main__":
    unittest.main()
