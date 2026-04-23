"""
noticer.py - 本地 Webhook 提醒服务插件
========================================

启动一个本地 HTTP 服务器，接收外部 HTTP 请求并通过机器人客户端发送消息到指定的 QQ 群聊。
适合在 CI/CD 流水线、定时任务、监控告警等场景中发送通知消息。

═══════════════════════════════════════════════════════════
  🔧 配置说明 (noticer.toml)
═══════════════════════════════════════════════════════════

配置文件位于插件配置目录下的 `noticer.toml`，结构如下：

```toml
[main]
host = "127.0.0.1"       # HTTP 监听地址，默认 127.0.0.1
port = 10020             # HTTP 监听端口，默认 10020

[rooms]
notice  = { id = -111111111, desc = "项目提醒群" }
warning = { id = -222222222 }

[rooms.bot]
desc = "bot 群, 基本随便发"
id = -12345
```

参数说明:

  [main]
    host   — 监听地址，建议保持 127.0.0.1 仅本地访问；如需公网访问请自行加反代/防火墙
    port   — 监听端口，确保不与其它服务冲突

  [rooms]
    每个房间使用内联表格式:
      - `notice = { id = -111111111, desc = "项目提醒群" }`
      - `warning = { id = -222222222 }`
        (不指定 desc 时自动生成 "{name} room")

═══════════════════════════════════════════════════════════
  📡 API 端点
═══════════════════════════════════════════════════════════

─────────────────────────────────
  GET  /
─────────────────────────────────

返回本 README 文档（即当前显示的内容），方便 curl 快速查阅使用说明。

  curl http://127.0.0.1:10020/

─────────────────────────────────
  GET  /status
─────────────────────────────────

返回服务器状态 JSON，包含所有已配置的房间列表及信息。

  curl http://127.0.0.1:10020/status

响应示例:
  {
    "server": "noticer/0.3.0",
    "status": "running",
    "client_ready": true,
    "rooms": {
      "notice": {
        "room_id": -111111111,
        "description": "项目提醒群",
        "configured": true
      },
      "warning": {
        "room_id": -222222222,
        "description": "warning room",
        "configured": true
      }
    }
  }

─────────────────────────────────
  GET  /health
─────────────────────────────────

简单健康检查，返回服务是否运行以及客户端是否就绪。

  curl http://127.0.0.1:10020/health

响应示例:
  {"status": "running", "client_ready": true}

─────────────────────────────────
  POST /send
─────────────────────────────────

发送消息到指定房间。请求体为 JSON 格式。

  curl -X POST http://127.0.0.1:10020/send \\
    -H "Content-Type: application/json" \\
    -d '{"room": "notice", "message": "🤖 任务已完成！\\n耗时: 12.3s"}'

请求体参数:
  room    (string, 必填) — 房间名称，可选值取决于配置文件 [rooms] 中定义的房间
  message (string, 必填) — 消息内容，支持 \\n 换行

成功响应:
  {"status": "ok", "room": "notice"}

错误响应:
  {"error": "错误描述信息"}

可能出现的错误:
  - 400: room 参数缺失 / 未知 room / message 为空 / 房间未配置
  - 404: 目标群不在机器人当前会话中（机器人可能未加入该群）
  - 503: 机器人客户端尚未就绪（未收到任何消息）
  - 500: 发送失败（内部错误）

═══════════════════════════════════════════════════════════
  💡 使用场景示例
═══════════════════════════════════════════════════════════

  # 查看服务状态
  curl http://127.0.0.1:10020/status

  # 发送提醒消息（如：构建成功）
  curl -X POST http://127.0.0.1:10020/send \\
    -H "Content-Type: application/json" \\
    -d '{"room": "notice", "message": "✅ 前端构建成功\\n分支: main\\n提交: a1b2c3d"}'

  # 发送警告消息（如：服务器负载过高）
  curl -X POST http://127.0.0.1:10020/send \\
    -H "Content-Type: application/json" \\
    -d '{"room": "warning", "message": "⚠️ 服务器告警\\nCPU: 95%\\n内存: 87%\\n请及时处理！"}'

  # CI 流水线中使用 (GitHub Actions / GitLab CI)
  curl -X POST http://127.0.0.1:10020/send \\
    -H "Content-Type: application/json" \\
    -d "{\\"room\\": \\"notice\\", \\"message\\": \\"🔨 CI 构建完成\\n项目: $CI_PROJECT_NAME\\n状态: $CI_JOB_STATUS\\"}"

═══════════════════════════════════════════════════════════
  🚀 拓展指南：添加自定义房间
═══════════════════════════════════════════════════════════

插件会自动发现配置文件中 [rooms] 区定义的所有房间，无需修改代码即可使用。

示例 — 添加一个调试房间:

  1. 在 noticer.toml 的 [rooms] 区添加:
       debug = { id = -333333333, desc = "调试群" }

  2. 重载插件，即可使用 room="debug" 发送消息。

desc 可选，不指定时自动生成为 "debug room"。

═══════════════════════════════════════════════════════════
  ❓ 常见问题
═══════════════════════════════════════════════════════════

  Q: 返回 404 "room not found in current session"?
  A: 请确保机器人已经加入了目标 QQ 群，且群号与配置文件中的一致。

  Q: 返回 503 "bot client not ready yet"?
  A: 插件启动后需要收到第一条消息才能捕获客户端实例。向机器人发送任意消息
     （或在群里 @机器人 发消息）即可初始化。

  Q: 如何新增自定义房间?
  A: 在配置文件的 [rooms] 下增加新的键值对即可，插件会自动发现。
     可使用内联表格式自定义 desc，不写则自动生成 "{name} room"。

"""

from __future__ import annotations

import builtins
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

from shenbot_api import PluginManifest, ConfigStorage, python_config_path

try:
    import tomllib                   # Python 3.11+ 标准库
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None               # fallback

# ============================================================
# 默认值常量（集中管理，改一处即可）
# ============================================================

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10020


# ============================================================
# 插件元信息
# ============================================================

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="noticer",
    name="Noticer 本地提醒服务",
    version="0.3.0",
    description="启动本地 HTTP 服务，接收外部请求并通过 bot 发送提醒/警告消息到指定群聊",
    authors=["your_name"],
    config={
        "main": ConfigStorage(
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
        ),
        "rooms": ConfigStorage(
            notice={"id": 0, "desc": ""},
            warning={"id": 0, "desc": ""},
        ),
    },
)

# ============================================================
# 全局状态
# ============================================================

ROOM_DESCRIPTIONS: dict[str, str] = {
    "notice":  "提醒房间（用于发送一般性提醒消息）",
    "warning": "警告房间（用于发送警告/异常消息）",
}
"""房间名 → 中文描述映射。仅作为 fallback，配置中的 desc 优先级更高。"""

MAX_BODY_SIZE = 256 * 1024
SOCKET_TIMEOUT = 10.0
RELOAD_NOTICE_TTL = 30.0
_RUNTIME_STATE_KEY = "_ica_noticer_runtime_state"
_RELOAD_COMMAND_RE = re.compile(r"^/bot-reload-\d+\s+noticer(?:\s+.*)?$")

_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None

_client_lock = threading.Lock()
_ica_client: IcaClient | None = None

_host: str = DEFAULT_HOST
_port: int = DEFAULT_PORT
_rooms: dict[str, int] = {}                 # room_name -> room_id
_room_descriptions: dict[str, str] = {}     # room_name -> desc (来自配置或自动生成)


def _runtime_state() -> dict[str, object]:
    """获取跨插件重载保留的运行时状态"""
    state = getattr(builtins, _RUNTIME_STATE_KEY, None)
    if not isinstance(state, dict):
        state = {
            "client": None,
            "pending_reload_room_id": None,
            "pending_reload_deadline": 0.0,
        }
        setattr(builtins, _RUNTIME_STATE_KEY, state)
    return state


def _get_persisted_client() -> IcaClient | None:
    client = _runtime_state().get("client")
    return cast("IcaClient | None", client)


def _set_persisted_client(client: IcaClient | None) -> None:
    _runtime_state()["client"] = client


def _set_pending_reload_notice(room_id: int) -> None:
    state = _runtime_state()
    state["pending_reload_room_id"] = room_id
    state["pending_reload_deadline"] = time.time() + RELOAD_NOTICE_TTL


def _get_pending_reload_notice() -> int | None:
    state = _runtime_state()
    room_id = state.get("pending_reload_room_id")
    deadline = state.get("pending_reload_deadline", 0.0)

    if not isinstance(room_id, int):
        return None
    if not isinstance(deadline, (int, float)) or time.time() > float(deadline):
        state["pending_reload_room_id"] = None
        state["pending_reload_deadline"] = 0.0
        return None
    return room_id


def _clear_pending_reload_notice() -> None:
    state = _runtime_state()
    state["pending_reload_room_id"] = None
    state["pending_reload_deadline"] = 0.0


def _find_room(client: IcaClient, room_id: int):
    """按 room_id 查找当前会话中的房间对象"""
    for room in client.status.rooms:
        if room.room_id == room_id:
            return room
    return None


def _send_text_to_room(client: IcaClient, room_id: int, text: str) -> bool:
    """向指定房间发送纯文本消息"""
    target_room = _find_room(client, room_id)
    if target_room is None:
        return False
    return bool(client.send_message(target_room.new_message_to(text)))


def _maybe_send_reload_notice(client: IcaClient) -> None:
    """如有待发送的重载完成通知，则立即发送"""
    room_id = _get_pending_reload_notice()
    if room_id is None:
        return

    try:
        ok = _send_text_to_room(
            client,
            room_id,
            f"✅ noticer 重载完成\n服务地址: http://{_host}:{_port}",
        )
        if ok:
            _clear_pending_reload_notice()
            _log_info(f"Reload completion notice sent to room {room_id}")
        else:
            _log_warn(f"Reload completion notice pending: room {room_id} not found yet")
    except Exception as e:
        _log_warn(f"Failed to send reload completion notice: {e}")


class _NoticerServer(ThreadingHTTPServer):
    """本地 webhook 服务"""
    daemon_threads = True
    allow_reuse_address = True


# ============================================================
# 日志辅助
# ============================================================

def _log_info(msg: str) -> None:
    """输出信息日志 — 优先走 bot 日志，fallback 到 print"""
    with _client_lock:
        client = _ica_client
    if client is not None:
        try:
            client.info(msg)
            return
        except Exception:
            pass
    print(f"[noticer] {msg}")


def _log_warn(msg: str) -> None:
    """输出警告日志"""
    with _client_lock:
        client = _ica_client
    if client is not None:
        try:
            client.warn(msg)
            return
        except Exception:
            pass
    print(f"[noticer] [WARN] {msg}")


# ============================================================
# 房间配置加载
# ============================================================

def _load_rooms() -> None:
    """
    直接从 TOML 配置文件的 [rooms] 区读取所有房间。

    使用内联表格式: ``notice = { id = -111111111, desc = "提醒群" }``

    未指定 desc 时自动生成 ``"{name} room"``。
    """
    global _rooms, _room_descriptions
    _rooms = {}
    _room_descriptions = {}

    # 直接从磁盘读取 TOML 配置文件，支持动态房间名
    config_path = os.path.join(python_config_path(), "noticer.toml")

    if tomllib is not None and os.path.isfile(config_path):
        try:
            with open(config_path, "rb") as f:
                parsed = tomllib.load(f)
            rooms_table = parsed.get("rooms", {})
            if not isinstance(rooms_table, dict):
                rooms_table = {}
            for name, value in rooms_table.items():
                if not isinstance(value, dict):
                    continue
                rid = int(value.get("id", 0))
                desc = value.get("desc", "")
                if rid != 0:
                    _rooms[name] = rid
                    _room_descriptions[name] = desc or f"{name} room"
            return  # 解析成功
        except Exception:
            pass  # 解析失败，走 fallback

    # ── fallback: 通过 ConfigStorage 逐 key 读取 ──
    # （仅对 ROOM_DESCRIPTIONS 中的已知房间生效）
    rooms_cfg = PLUGIN_MANIFEST.config_unchecked("rooms")
    for name in ROOM_DESCRIPTIONS:
        try:
            value = rooms_cfg.get_value(name)
        except Exception:
            continue

        if isinstance(value, dict):
            rid = int(value.get("id", 0))
            desc = value.get("desc", "")
        else:
            continue

        if rid != 0:
            _rooms[name] = rid
            _room_descriptions[name] = desc or f"{name} room"



def _get_room_id(room_name: str) -> int:
    """根据房间名获取群 ID，未找到返回 0"""
    return _rooms.get(room_name, 0)


def _get_room_description(room_name: str) -> str:
    """
    获取房间的描述文本。

    优先级:
      1. 配置中指定的 desc（或自动生成的 "{name} room"）
      2. ROOM_DESCRIPTIONS 中的中文描述（硬编码 fallback）
      3. 兜底: "{name} room"
    """
    if room_name in _room_descriptions:
        return _room_descriptions[room_name]
    if room_name in ROOM_DESCRIPTIONS:
        return ROOM_DESCRIPTIONS[room_name]
    return f"{room_name} room"


def _get_room_names_str() -> str:
    """返回所有可用房间名的逗号分隔字符串，用于错误提示"""
    if _rooms:
        return ", ".join(sorted(_rooms.keys()))
    return ", ".join(ROOM_DESCRIPTIONS.keys())


# ============================================================
# HTTP Handler
# ============================================================

class _NoticerHandler(BaseHTTPRequestHandler):
    """处理 webhook HTTP 请求"""

    # ---------- 路由 ----------

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT)

    def handle(self) -> None:
        """处理单个请求后关闭连接，避免 keep-alive 导致 curl 卡住"""
        self.close_connection = True
        self.handle_one_request()

    def do_GET(self) -> None:
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._handle_root()
        elif parsed.path == "/status":
            self._handle_status()
        elif parsed.path == "/health":
            self._handle_health()
        else:
            self._send_json(404, {"error": f"not found: {self.path}"})

    def do_POST(self) -> None:
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        if parsed.path == "/send":
            self._handle_send()
        else:
            self._send_json(404, {"error": f"not found: {self.path}"})

    # ---------- GET / ----------

    def _handle_root(self) -> None:
        """返回 README 文档（模块 docstring），方便 curl 查阅"""
        body = __doc__.encode("utf-8") if __doc__ else b""
        self.close_connection = True
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8") + body
        self.request.sendall(response)

    # ---------- GET /status ----------

    def _handle_status(self) -> None:
        """返回服务状态 + 所有已配置房间列表 JSON"""
        rooms = {}
        for name, rid in _rooms.items():
            rooms[name] = {
                "room_id": rid,
                "description": _get_room_description(name),
                "configured": True,
            }
        # 也包含 ROOM_DESCRIPTIONS 中定义了但未配置的房间（room_id=0，configured=False）
        for name in ROOM_DESCRIPTIONS:
            if name not in _rooms:
                rooms[name] = {
                    "room_id": 0,
                    "description": ROOM_DESCRIPTIONS[name],
                    "configured": False,
                }

        with _client_lock:
            client_ready = _ica_client is not None

        self._send_json(200, {
            "server": f"noticer/{PLUGIN_MANIFEST.version}",
            "status": "running",
            "client_ready": client_ready,
            "rooms": rooms,
        })

    # ---------- GET /health ----------

    def _handle_health(self) -> None:
        with _client_lock:
            client_ready = _ica_client is not None
        self._send_json(200, {
            "status": "running",
            "client_ready": client_ready,
        })

    # ---------- POST /send ----------

    def _handle_send(self) -> None:
        """处理发送消息请求"""
        remote = self.client_address[0]

        # 1. 校验并读取 body
        content_len_raw = self.headers.get("Content-Length")
        try:
            content_len = int(content_len_raw or "0")
        except (TypeError, ValueError):
            self._log_and_response(remote, "", "", 400, "invalid Content-Length")
            return

        if content_len <= 0:
            self._log_and_response(remote, "", "", 400, "empty body")
            return

        if content_len > MAX_BODY_SIZE:
            self._log_and_response(
                remote,
                "",
                "",
                413,
                f"request body too large (max {MAX_BODY_SIZE} bytes)",
            )
            return

        try:
            raw = self.rfile.read(content_len)
        except (socket.timeout, TimeoutError):
            self._log_and_response(remote, "", "", 408, "request body read timeout")
            return
        except OSError as e:
            self._log_and_response(remote, "", "", 400, f"failed to read body: {e}")
            return

        if len(raw) != content_len:
            self._log_and_response(remote, "", "", 400, "incomplete request body")
            return

        try:
            data = json.loads(raw)
        except Exception as e:
            self._log_and_response(remote, "?", "?", 400, f"invalid json: {e}")
            return

        if not isinstance(data, dict):
            self._log_and_response(remote, "?", "?", 400, "json body must be an object")
            return

        # 2. 取参 + 校验类型
        room_name = data.get("room")
        message = data.get("message")

        if not isinstance(room_name, str) or not room_name.strip():
            self._log_and_response(
                remote,
                "" if room_name is None else self._msg_preview(room_name),
                self._msg_preview(message),
                400,
                "`room` is required (available: " + _get_room_names_str() + ")",
            )
            return
        room_name = room_name.strip()

        if not isinstance(message, str):
            self._log_and_response(
                remote,
                room_name,
                self._msg_preview(message),
                400,
                "`message` must be a string",
            )
            return

        if room_name not in _rooms:
            self._log_and_response(remote, room_name, self._msg_preview(message), 400,
                                   f"unknown room '{room_name}', available: " +
                                   _get_room_names_str())
            return

        if message == "":
            self._log_and_response(remote, room_name, "", 400, "`message` is required")
            return

        # 3. 获取房间 ID
        room_id = _get_room_id(room_name)
        if room_id == 0:
            self._log_and_response(remote, room_name, self._msg_preview(message), 400,
                                   f"room '{room_name}' is not configured (room_id = 0)")
            return

        # 4. 获取 client
        with _client_lock:
            client = _ica_client
        if client is None:
            self._log_and_response(remote, room_name, self._msg_preview(message), 503,
                                   "bot client not ready yet")
            return

        # 5. 查找 room 对象并发消息
        try:
            target_room = _find_room(client, room_id)

            if target_room is None:
                self._log_and_response(remote, room_name, self._msg_preview(message), 404,
                                       f"room {room_id} not found in current session "
                                       "(bot may not have joined this group)")
                return

            send_msg = target_room.new_message_to(message)
            ok = client.send_message(send_msg)

            if ok:
                self._log_and_response(remote, room_name, self._msg_preview(message), 200,
                                       "ok")
            else:
                self._log_and_response(remote, room_name, self._msg_preview(message), 500,
                                       "send_message returned false")

        except Exception as e:
            self._log_and_response(remote, room_name, self._msg_preview(message), 500,
                                   str(e))

    # ---------- 辅助方法 ----------

    @staticmethod
    def _msg_preview(message: object, max_len: int = 60) -> str:
        """截取消息预览，用于日志"""
        if message is None:
            return ""
        preview = message if isinstance(message, str) else repr(message)
        preview = preview.replace("\n", "\\n").replace("\r", "\\r")
        if len(preview) > max_len:
            preview = preview[:max_len] + "..."
        return preview

    def _log_and_response(self, remote: str, room_name: str, msg_preview: str,
                          status: int, detail: str) -> None:
        """统一的日志 + 响应"""
        # 日志
        log_line = (
            f"POST /send from {remote} "
            f"→ room=\"{room_name}\" msg=\"{msg_preview}\" "
            f"→ {status} {detail}"
        )
        if 200 <= status < 300:
            _log_info(log_line)
        else:
            _log_warn(log_line)

        # 响应
        body = {} if status == 200 else {"error": detail}
        if status == 200:
            body = {"status": "ok", "room": room_name}
        self._send_json(status, body)

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        status_text = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            408: "Request Timeout",
            413: "Payload Too Large",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status, "Unknown")
        self.close_connection = True
        response = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8") + body
        self.request.sendall(response)

    def log_message(self, format: str, *args) -> None:
        """抑制 http.server 默认的 stderr 日志，由我们自己接管"""
        pass


# ============================================================
# 插件生命周期
# ============================================================

def on_load() -> None:
    """插件加载时 — 读取配置 + 启动 HTTP server"""
    global _server, _server_thread, _host, _port, _ica_client

    # 读取配置 — [main] 部分
    main_cfg = PLUGIN_MANIFEST.config_unchecked("main")

    raw_host: Any = main_cfg.get_value("host")
    _host = DEFAULT_HOST if raw_host in (None, "") else str(raw_host)

    raw_port: Any = main_cfg.get_value("port")
    try:
        _port = int(raw_port) if raw_port not in (None, "") else DEFAULT_PORT
    except (TypeError, ValueError):
        _port = DEFAULT_PORT

    # 动态读取所有房间 — [rooms] 部分
    _load_rooms()

    with _client_lock:
        _ica_client = _get_persisted_client()

    # 启动 HTTP server (daemon 线程, 主线程退出时自动结束)
    try:
        _server = _NoticerServer((_host, _port), _NoticerHandler)
        _server_thread = threading.Thread(
            target=_server.serve_forever,
            daemon=True,
        )
        _server_thread.start()

        if _rooms:
            rooms_info = [
                f"  {name}: {rid} ({_get_room_description(name)})"
                for name, rid in _rooms.items()
            ]
            rooms_str = "\n".join(rooms_info)
        else:
            rooms_str = "  (no rooms configured)"

        _log_info(
            f"HTTP server started on {_host}:{_port}\n"
            + rooms_str
        )

        with _client_lock:
            client = _ica_client
        if client is not None:
            _maybe_send_reload_notice(client)
    except Exception as e:
        print(f"[noticer] Failed to start HTTP server: {e}")


def on_unload() -> None:
    """插件卸载时 — 关闭 HTTP server"""
    global _server, _server_thread
    server = _server
    thread = _server_thread
    _server = None
    _server_thread = None

    if server is not None:
        server.shutdown()
        server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        print("[noticer] HTTP server stopped")


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    """捕获 client 实例 + 响应 /noticer 命令"""
    global _ica_client

    client_updated = False
    with _client_lock:
        current_client_id = _ica_client.client_id if _ica_client is not None else None
        new_client_id = client.client_id
        if current_client_id != new_client_id:
            _ica_client = client
            _set_persisted_client(client)
            client_updated = True
        elif _ica_client is None:
            _ica_client = client
            _set_persisted_client(client)

    if client_updated:
        _log_info("Client captured, webhook ready")

    if msg.content and _RELOAD_COMMAND_RE.fullmatch(msg.content.strip()):
        _set_pending_reload_notice(int(msg.room_id))
        return

    _maybe_send_reload_notice(client)

    # 消息处理
    if msg.is_from_self or msg.is_reply:
        return

    if msg.content == "/noticer":
        _show_status(msg, client)
    elif msg.content == "/noticer help":
        _show_help(msg, client)


def _show_status(msg: IcaNewMessage, client: IcaClient) -> None:
    """显示 noticer 插件状态"""
    if _rooms:
        rooms_info = [
            f"{name}: ✅ {rid} ({_get_room_description(name)})"
            for name, rid in _rooms.items()
        ]
    else:
        rooms_info = ["(暂无已配置的房间，请在配置文件中设置 [rooms])"]

    with _client_lock:
        client_ready = _ica_client is not None

    info = (
        "📡 Noticer 本地提醒服务\n"
        f"服务地址: http://{_host}:{_port}\n"
        f"客户端就绪: {'✅' if client_ready else '❌'}\n"
        "---\n"
        + "\n".join(rooms_info)
    )
    client.send_message(msg.reply_with(info))


def _show_help(msg: IcaNewMessage, client: IcaClient) -> None:
    """显示帮助"""
    rooms_text = (
        "\n".join(
            f"  {name} - {_get_room_description(name)}"
            for name in sorted(_rooms.keys()) if _rooms[name] != 0
        )
        if _rooms else
        "  (暂无已配置的房间)"
    )
    help_text = (
        "📖 Noticer 使用帮助\n"
        f"发送: curl -X POST http://{_host}:{_port}/send \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -d '{\"room\": \"notice\", \"message\": \"hello\"}'\n"
        f"文档: curl http://{_host}:{_port}/\n"
        f"状态: curl http://{_host}:{_port}/status\n"
        "可用 room 名:\n"
        f"{rooms_text}"
    )
    client.send_message(msg.reply_with(help_text))
