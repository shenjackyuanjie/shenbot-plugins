"""
noticer.py - 本地 Webhook 提醒服务插件

启动本地 HTTP 服务，接收外部请求并通过 bot 发送消息到指定 QQ 群。
适用于 CI/CD、定时任务、监控告警等场景。

# 配置 (noticer.toml)

```toml
[main]
host = "127.0.0.1"   # 监听地址，默认 127.0.0.1
port = 10020         # 监听端口，默认 10020

[rooms]
notice  = { id = -111111111, desc = "项目提醒群" }
warning = { id = -222222222 }
# 可自由添加更多房间，desc 不填则自动生成 "{name} room"
```

# API

💡 平台注意事项：
  • **PowerShell**：`curl` 是 `Invoke-WebRequest` 的别名，参数不兼容。请用 `curl.exe`。
  • **Git Bash / msys2**：`curl.exe` 可用，但终端输出编码是 GBK，含中文/Emoji 时会乱码。
  • **Windows Terminal + PowerShell 7**：`curl.exe` 可直接写中文/Emoji，无编码问题。
  • **Python**（任何平台）：完全无编码问题，推荐优先使用（见下方 POST /send 示例）。

## GET / — 返回本文档
  curl http://127.0.0.1:10020/

## GET /status — 服务状态（含房间列表）
  curl http://127.0.0.1:10020/status
  → {"server":"noticer/0.3.2","status":"running","client_ready":true,"rooms":{...}}

## GET /health — 健康检查
  curl http://127.0.0.1:10020/health
  → {"status":"running","client_ready":true}

## POST /send — 发送消息

  # 方式 A：写文件 + curl（跨平台，推荐）
  python -c "open('payload.json','w',encoding='utf-8').write('{\"room\":\"notice\",\"message\":\"🤖 任务完成！\\\\n耗时: 12.3s\"}')"
  curl -X POST http://127.0.0.1:10020/send -H "Content-Type: application/json" -d @payload.json

  # 方式 B：纯 Python（最可靠，一行搞定）
  python -c "import urllib.request,json; d=json.dumps({'room':'notice','message':'🤖 任务完成！\\n耗时: 12.3s'}).encode(); r=urllib.request.Request('http://127.0.0.1:10020/send',data=d,headers={'Content-Type':'application/json'},method='POST'); print(urllib.request.urlopen(r).read().decode())"

  参数:
    room         - 房间名（必填，见 [rooms] 配置）
    message      - 消息内容（可选，支持 \\n 换行；不发图片时必填）
    image        - 图片（可选）。可传 data URL/base64 字符串，或对象：
                   {"base64":"...", "type":"image/png", "as_sticker":false}
    image_base64 - 图片 base64（可选，等价于 image；需配合 image_type）
    image_type   - 图片 MIME，默认 image/png；常用 image/png 或 image/jpeg
    as_sticker   - 是否作为贴纸发送，默认 false

  成功: {"status":"ok","room":"notice"}
  失败: {"error":"描述信息"}

  错误码:
    400 - room 缺失/未知/message 和 image 均为空/图片参数无效/房间未配置
    404 - bot 未加入该群
    503 - 客户端未就绪（需先向 bot 发条消息初始化）
    500 - 发送失败

# 使用示例

  # 查看状态
  curl http://127.0.0.1:10020/status

  # 构建成功通知（Linux/macOS/WSL，终端为 UTF-8，可直接 inline）
  curl -X POST http://127.0.0.1:10020/send -H "Content-Type: application/json" \
    -d '{"room":"notice","message":"✅ 构建成功\\n分支: main\\n提交: a1b2c3d"}'

  # 构建成功通知（纯 Python，任何平台都行）
  python -c "import urllib.request,json; d=json.dumps({'room':'notice','message':'✅ 构建成功！\\n耗时: 12.3s'}).encode(); r=urllib.request.Request('http://127.0.0.1:10020/send',data=d,headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(r).read().decode())"

  # 发送图片（base64，可带文字）
  python -c "import base64,json,urllib.request; img=base64.b64encode(open('report.png','rb').read()).decode(); d=json.dumps({'room':'notice','message':'日报图表','image':{'base64':img,'type':'image/png'}}).encode(); r=urllib.request.Request('http://127.0.0.1:10020/send',data=d,headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(r).read().decode())"

  # 发送图片（data URL）
  python -c "import base64,json,urllib.request; img='data:image/png;base64,'+base64.b64encode(open('report.png','rb').read()).decode(); d=json.dumps({'room':'notice','image':img}).encode(); r=urllib.request.Request('http://127.0.0.1:10020/send',data=d,headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(r).read().decode())"

  # 服务器告警（用 Python 写文件，绕过所有编码问题）
  python -c "open('payload.json','w',encoding='utf-8').write('{\"room\":\"warning\",\"message\":\"⚠️ 服务器告警\\\\nCPU: 95%\\\\n内存: 87%\"}')"
  curl -X POST http://127.0.0.1:10020/send -H "Content-Type: application/json" -d @payload.json

# ⚠️ Windows / GBK 编码注意事项

Windows 的 cmd/PowerShell 默认编码为 GBK，直接在 curl -d 参数中写中文/Emoji
会导致服务端 UTF-8 JSON 解析失败：
  invalid json: 'utf-8' codec can't decode byte ...

解决办法：

  1) 纯 Python 发送（推荐，各平台通用，无编码问题）
       python -c "import urllib.request,json; d=json.dumps({'room':'notice','message':'你好 ✅'}).encode(); r=urllib.request.Request('http://127.0.0.1:10020/send',data=d,headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(r).read().decode())"

  2) 用 Python 写 UTF-8 文件 + curl 发送（需要 curl 的场景）
       Windows 的 echo 输出的是 GBK 而非 UTF-8，不要用 echo 写 JSON 文件。
       用 Python 确保文件编码正确：

       python -c "open('payload.json','w',encoding='utf-8').write('{\"room\":\"notice\",\"message\":\"你好 ✅\"}')"
       curl -X POST http://127.0.0.1:10020/send -H "Content-Type: application/json" -d @payload.json

  3) 用 PowerShell Invoke-RestMethod（仅限 PowerShell）
       $body = @{ room="notice"; message="你好 ✅" } | ConvertTo-Json
       Invoke-RestMethod -Uri http://127.0.0.1:10020/send -Method Post -Body $body -ContentType "application/json"

# FAQ

  Q: 404 "room not found in current session"?
  A: 确保 bot 已加入目标群，且群号与配置一致。

  Q: 503 "bot client not ready yet"?
  A: 向 bot 发任意消息（或群里 @bot）即可初始化客户端。

  Q: 如何新增自定义房间?
  A: 在 noticer.toml 的 [rooms] 下添加键值对即可，插件自动发现。

  Q: curl 报 utf-8 decode 错误?
  A: Windows 终端编码问题。见上方「⚠️ Windows / GBK 编码注意事项」章节。
"""

from __future__ import annotations

import base64
import binascii
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
    version="0.3.2",
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

MAX_BODY_SIZE = 10 * 1024 * 1024
MAX_IMAGE_SIZE = 8 * 1024 * 1024
SOCKET_TIMEOUT = 10.0
RELOAD_NOTICE_TTL = 30.0
_RUNTIME_STATE_KEY = "_ica_noticer_runtime_state"
_RELOAD_COMMAND_RE = re.compile(r"^/bot-reload-\d+\s+noticer(?:\s+.*)?$")
_DATA_IMAGE_RE = re.compile(
    r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_ALLOWED_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
}

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


def _normalize_image_type(file_type: object) -> str:
    if not isinstance(file_type, str) or not file_type.strip():
        return "image/png"
    file_type = file_type.strip().lower()
    if file_type == "image/jpg":
        return "image/jpeg"
    return file_type


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _decode_base64_image(raw_image: str, fallback_type: object) -> tuple[bytes, str, str | None]:
    file_type = _normalize_image_type(fallback_type)
    image_text = raw_image.strip()
    match = _DATA_IMAGE_RE.fullmatch(image_text)
    if match is not None:
        file_type = _normalize_image_type(match.group(1))
        image_text = match.group(2).strip()
    image_text = re.sub(r"\s+", "", image_text)

    if file_type not in _ALLOWED_IMAGE_TYPES:
        return b"", file_type, (
            "`image_type` must be one of: "
            + ", ".join(sorted(_ALLOWED_IMAGE_TYPES))
        )

    try:
        image_bytes = base64.b64decode(image_text, validate=True)
    except (binascii.Error, ValueError) as e:
        return b"", file_type, f"invalid image base64: {e}"

    if not image_bytes:
        return b"", file_type, "image is empty"
    if len(image_bytes) > MAX_IMAGE_SIZE:
        return b"", file_type, f"image too large (max {MAX_IMAGE_SIZE} bytes)"
    return image_bytes, file_type, None


def _parse_image_payload(data: dict[str, object]) -> tuple[bytes | None, str, bool, str | None]:
    """解析 JSON 图片参数。返回 (bytes, mime, as_sticker, error)。"""
    image_payload = data.get("image")
    top_level_base64 = data.get("image_base64")
    file_type: object = data.get("image_type")
    as_sticker = _parse_bool(data.get("as_sticker"), False)

    if image_payload is None and top_level_base64 is None:
        return None, "image/png", as_sticker, None

    if isinstance(image_payload, dict):
        base64_payload = (
            image_payload.get("base64")
            or image_payload.get("data")
            or image_payload.get("content")
        )
        file_type = (
            image_payload.get("type")
            or image_payload.get("mime")
            or image_payload.get("file_type")
            or file_type
        )
        as_sticker = _parse_bool(image_payload.get("as_sticker"), as_sticker)
    else:
        base64_payload = image_payload if image_payload is not None else top_level_base64

    if not isinstance(base64_payload, str) or not base64_payload.strip():
        return None, _normalize_image_type(file_type), as_sticker, (
            "`image` must be a base64/data-url string or an object with `base64`"
        )

    image_bytes, normalized_type, error = _decode_base64_image(base64_payload, file_type)
    if error is not None:
        return None, normalized_type, as_sticker, error
    return image_bytes, normalized_type, as_sticker, None


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
        room_id_raw = data.get("room_id")
        message = data.get("message")
        image_bytes, image_type, as_sticker, image_error = _parse_image_payload(data)

        # room_id 优先（直接指定群号），否则用 room 名称查找
        if isinstance(room_id_raw, (int, float)):
            room_id = int(room_id_raw)
            room_name = str(room_name).strip() if isinstance(room_name, str) else f"room_{room_id}"
        elif isinstance(room_name, str) and room_name.strip():
            room_name = room_name.strip()
            if room_name not in _rooms:
                self._log_and_response(remote, room_name, self._msg_preview(message), 400,
                                       f"unknown room '{room_name}', available: " +
                                       _get_room_names_str())
                return
            room_id = _get_room_id(room_name)
            if room_id == 0:
                self._log_and_response(remote, room_name, self._msg_preview(message), 400,
                                       f"room '{room_name}' is not configured (room_id = 0)")
                return
        else:
            self._log_and_response(
                remote,
                "" if room_name is None else self._msg_preview(room_name),
                self._msg_preview(message),
                400,
                "`room` or `room_id` is required (available rooms: " + _get_room_names_str() + ")",
            )
            return

        if message is None:
            message = ""
        if not isinstance(message, str):
            self._log_and_response(
                remote,
                room_name,
                self._msg_preview(message),
                400,
                "`message` must be a string",
            )
            return

        if image_error is not None:
            self._log_and_response(
                remote,
                room_name,
                self._msg_preview(message),
                400,
                image_error,
            )
            return

        if message == "" and image_bytes is None:
            self._log_and_response(
                remote,
                room_name,
                "",
                400,
                "`message` or `image` is required",
            )
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
            if image_bytes is not None:
                send_msg.set_img(image_bytes, image_type, as_sticker)
            ok = client.send_message(send_msg)

            preview = self._msg_preview_with_image(message, image_bytes, image_type)
            if ok:
                self._log_and_response(remote, room_name, preview, 200,
                                       "ok")
            else:
                self._log_and_response(remote, room_name, preview, 500,
                                       "send_message returned false")

        except Exception as e:
            self._log_and_response(
                remote,
                room_name,
                self._msg_preview_with_image(message, image_bytes, image_type),
                500,
                str(e),
            )

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

    @classmethod
    def _msg_preview_with_image(
        cls,
        message: object,
        image_bytes: bytes | None,
        image_type: str,
    ) -> str:
        preview = cls._msg_preview(message)
        if image_bytes is None:
            return preview
        image_preview = f"[image {image_type} {len(image_bytes)} bytes]"
        if preview:
            return f"{preview} {image_preview}"
        return image_preview

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
        "图片: JSON 可加 image 字段，支持 data URL 或 base64 对象\n"
        "  {\"room\":\"notice\",\"message\":\"日报\",\"image\":{\"base64\":\"...\",\"type\":\"image/png\"}}\n"
        f"文档: curl http://{_host}:{_port}/\n"
        f"状态: curl http://{_host}:{_port}/status\n"
        "可用 room 名:\n"
        f"{rooms_text}"
    )
    client.send_message(msg.reply_with(help_text))
