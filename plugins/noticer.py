"""
noticer.py - 本地 Webhook 提醒服务插件

启动本地 HTTP 服务，接收外部请求并通过 bot 发送消息到指定 QQ 群。
适用于 CI/CD、定时任务、监控告警等场景。

# 配置 (noticer.toml)

```toml
[main]
host = "127.0.0.1"   # 监听地址，默认 127.0.0.1
port = 10020         # 监听端口，默认 10020
auth_token = ""      # 可选：保护 /send、/v1/send、/status
direct_token = ""    # 必填后才启用 /v1/send/direct
queue_capacity = 256
send_timeout_seconds = 60
retry_attempts = 3       # send_message 失败后的重试次数（含首次），默认 3
retry_delay_seconds = 1.0  # 每次重试前的固定等待秒数，默认 1.0

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
  → {"server":"noticer/0.4.0","status":"running","client_ready":true,"rooms":{...}}

## GET /health — 健康检查
  curl http://127.0.0.1:10020/health
  → {"status":"running","client_ready":true}

## GET /ready — 就绪检查；未捕获 bot client 或队列未启动时返回 503

## POST /send — 兼容发送接口

  # 方式 A：写文件 + curl（跨平台，推荐）
  python -c "open('payload.json','w',encoding='utf-8').write('{\"room\":\"notice\",\"message\":\"🤖 任务完成！\\\\n耗时: 12.3s\"}')"
  curl -X POST http://127.0.0.1:10020/send -H "Content-Type: application/json" -d @payload.json

  # 方式 B：纯 Python（最可靠，一行搞定）
  python -c "import urllib.request,json; d=json.dumps({'room':'notice','message':'🤖 任务完成！\\n耗时: 12.3s'}).encode(); r=urllib.request.Request('http://127.0.0.1:10020/send',data=d,headers={'Content-Type':'application/json'},method='POST'); print(urllib.request.urlopen(r).read().decode())"

  参数:
    room         - 房间名（必填，见 [rooms] 配置）
    room_id      - 0.4 仅为兼容保留，已弃用；请迁移到 /v1/send/direct
    message      - 消息内容（可选，支持 \\n 换行；不发图片时必填）
    image        - 图片（可选）。可传 data URL/base64 字符串，或对象：
                   {"base64":"...", "type":"image/png", "as_sticker":false}
    image_base64 - 图片 base64（可选，等价于 image；需配合 image_type）
    image_type   - 图片 MIME，默认 image/png；常用 image/png 或 image/jpeg
    as_sticker   - 是否作为贴纸发送，默认 false

  成功: {"status":"ok","room":"notice"}
  失败: {"error":"描述信息"}

## POST /v1/send — 严格房间名接口
  只接受配置中的 room 名；成功响应包含 request_id。
  可选请求头 Idempotency-Key，用于安全重试。

## POST /v1/send/direct — 任意当前会话接口
  只接受整数 room_id，且必须配置 direct_token 并携带：
    Authorization: Bearer <direct_token>
  文本、图片与贴纸字段和 /v1/send 相同。

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

  Q: direct API 返回 503?
  A: 在 noticer.toml 配置 direct_token 并重载插件；调用方必须携带同值 Bearer Token。

  Q: curl 报 utf-8 decode 错误?
  A: Windows 终端编码问题。见上方「⚠️ Windows / GBK 编码注意事项」章节。
"""

from __future__ import annotations

import base64
import binascii
import builtins
import hashlib
import hmac
import json
import math
import os
import queue
import re
import socket
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
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
    version="0.4.1",
    description="启动本地 HTTP 服务，接收外部请求并通过 bot 发送提醒/警告消息到指定群聊",
    authors=["shenjack"],
    config={
        "main": ConfigStorage(
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
            auth_token="",
            direct_token="",
            queue_capacity=256,
            send_timeout_seconds=60,
            retry_attempts=3,
            retry_delay_seconds=1.0,
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

MAX_BODY_SIZE = 12 * 1024 * 1024
MAX_IMAGE_SIZE = 8 * 1024 * 1024
SOCKET_TIMEOUT = 10.0
RELOAD_NOTICE_TTL = 30.0
DEFAULT_QUEUE_CAPACITY = 256
DEFAULT_SEND_TIMEOUT_SECONDS = 60.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 1.0
IDEMPOTENCY_TTL_SECONDS = 10 * 60.0
IDEMPOTENCY_MAX_ENTRIES = 2048
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
_auth_token = ""
_direct_token = ""
_queue_capacity = DEFAULT_QUEUE_CAPACITY
_send_timeout_seconds = DEFAULT_SEND_TIMEOUT_SECONDS
_retry_attempts = DEFAULT_RETRY_ATTEMPTS
_retry_delay_seconds = DEFAULT_RETRY_DELAY_SECONDS
_rooms: dict[str, int] = {}                 # room_name -> room_id
_room_descriptions: dict[str, str] = {}     # room_name -> desc (来自配置或自动生成)


@dataclass
class _SendJob:
    request_id: str
    room_name: str
    room_id: int
    message: str
    image_bytes: bytes | None
    image_type: str
    as_sticker: bool
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    started: bool = False
    cancelled: bool = False
    status: int = 500
    detail: str = "send failed"


@dataclass
class _IdempotencyEntry:
    fingerprint: str
    job: _SendJob
    expires_at: float


_send_queue: queue.Queue[_SendJob | None] | None = None
_send_worker_thread: threading.Thread | None = None
_send_accepting = False
_send_state_lock = threading.Lock()
_idempotency_lock = threading.Lock()
_idempotency_entries: OrderedDict[str, _IdempotencyEntry] = OrderedDict()
_reload_notice_lock = threading.Lock()


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


def _run_send_job(job: _SendJob) -> None:
    with job.lock:
        if job.cancelled:
            job.done.set()
            return
        job.started = True

    with _client_lock:
        client = _ica_client

    status = 500
    detail = "send failed"
    if client is None:
        status = 503
        detail = "bot client not ready yet"
    else:
        try:
            target_room = _find_room(client, job.room_id)
            if target_room is None:
                status = 404
                detail = (
                    f"room {job.room_id} not found in current session "
                    "(bot may not have joined this group)"
                )
            else:
                send_msg = target_room.new_message_to(job.message)
                if job.image_bytes is not None:
                    send_msg.set_img(job.image_bytes, job.image_type, job.as_sticker)
                attempts = max(1, int(_retry_attempts))
                for attempt in range(1, attempts + 1):
                    try:
                        sent = bool(client.send_message(send_msg))
                    except Exception as exc:
                        _log_warn(
                            f"request_id={job.request_id} target={job.room_name!r} "
                            f"send raised {type(exc).__name__} "
                            f"(attempt {attempt}/{attempts})"
                        )
                        sent = False
                    if sent:
                        status = 200
                        detail = "ok"
                        break
                    status = 500
                    detail = (
                        f"send_message returned false "
                        f"(attempt {attempt}/{attempts})"
                    )
                    if attempt < attempts:
                        _log_warn(
                            f"request_id={job.request_id} target={job.room_name!r} "
                            f"发送失败 (attempt {attempt}/{attempts})，"
                            f"{_retry_delay_seconds:g}s 后重试"
                        )
                        time.sleep(_retry_delay_seconds)
        except Exception as exc:
            _log_warn(
                f"request_id={job.request_id} target={job.room_name!r} "
                f"send raised {type(exc).__name__}"
            )
            status = 500
            detail = "send failed"

    with job.lock:
        job.status = status
        job.detail = detail
        job.done.set()


def _send_worker_loop(send_queue: queue.Queue[_SendJob | None]) -> None:
    while True:
        job = send_queue.get()
        try:
            if job is None:
                return
            _run_send_job(job)
        finally:
            send_queue.task_done()


def _start_send_worker() -> None:
    global _send_queue, _send_worker_thread, _send_accepting
    send_queue: queue.Queue[_SendJob | None] = queue.Queue(maxsize=_queue_capacity)
    worker = threading.Thread(
        target=_send_worker_loop,
        args=(send_queue,),
        name="noticer-send-worker",
        daemon=True,
    )
    with _send_state_lock:
        _send_queue = send_queue
        _send_worker_thread = worker
        _send_accepting = True
    worker.start()


def _stop_send_worker() -> None:
    global _send_queue, _send_worker_thread, _send_accepting
    with _send_state_lock:
        _send_accepting = False
        send_queue = _send_queue
        worker = _send_worker_thread

    if send_queue is None:
        return

    while True:
        try:
            pending = send_queue.get_nowait()
        except queue.Empty:
            break
        try:
            if pending is not None:
                with pending.lock:
                    if not pending.started:
                        pending.cancelled = True
                        pending.status = 503
                        pending.detail = "service is stopping"
                        pending.done.set()
        finally:
            send_queue.task_done()

    try:
        send_queue.put_nowait(None)
    except queue.Full:
        pass
    if worker is not None and worker.is_alive():
        worker.join(timeout=_send_timeout_seconds)
        if worker.is_alive():
            _log_warn("send worker did not stop before timeout; in-flight outcome unknown")

    with _send_state_lock:
        if _send_queue is send_queue:
            _send_queue = None
            _send_worker_thread = None


def _enqueue_job(job: _SendJob) -> tuple[bool, str]:
    with _send_state_lock:
        if not _send_accepting or _send_queue is None:
            return False, "send queue is not available"
        send_queue = _send_queue
        try:
            send_queue.put_nowait(job)
        except queue.Full:
            return False, "send queue is full"
    return True, ""


def _wait_for_job(job: _SendJob) -> tuple[int, str]:
    if job.done.wait(timeout=_send_timeout_seconds):
        with job.lock:
            return job.status, job.detail

    with job.lock:
        if not job.started:
            job.cancelled = True
            job.status = 504
            job.detail = "queue wait timeout; message was not sent"
            job.done.set()
            return job.status, job.detail
    return 504, "send timeout; outcome unknown"


def _queue_send(job: _SendJob) -> tuple[int, str]:
    enqueued, detail = _enqueue_job(job)
    if not enqueued:
        return 503, detail
    return _wait_for_job(job)


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


def _detect_image_type(image_bytes: bytes) -> str | None:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        len(image_bytes) >= 12
        and image_bytes.startswith(b"RIFF")
        and image_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"
    return None


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
    detected_type = _detect_image_type(image_bytes)
    if detected_type is None:
        return b"", file_type, "unsupported or malformed image data"
    if detected_type != file_type:
        return b"", file_type, (
            f"image MIME mismatch: declared {file_type}, detected {detected_type}"
        )
    return image_bytes, file_type, None


def _parse_image_payload(
    data: dict[str, object],
    *,
    strict: bool = False,
) -> tuple[bytes | None, str, bool, str | None]:
    """解析 JSON 图片参数。返回 (bytes, mime, as_sticker, error)。"""
    image_payload = data.get("image")
    top_level_base64 = data.get("image_base64")
    file_type: object = data.get("image_type")
    raw_as_sticker = data.get("as_sticker")
    if strict and raw_as_sticker is not None and not isinstance(raw_as_sticker, bool):
        return None, "image/png", False, "`as_sticker` must be a boolean"
    as_sticker = _parse_bool(raw_as_sticker, False)

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
        nested_as_sticker = image_payload.get("as_sticker")
        if (
            strict
            and nested_as_sticker is not None
            and not isinstance(nested_as_sticker, bool)
        ):
            return None, _normalize_image_type(file_type), as_sticker, (
                "`image.as_sticker` must be a boolean"
            )
        as_sticker = _parse_bool(nested_as_sticker, as_sticker)
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
    with _reload_notice_lock:
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
        except Exception as exc:
            _log_warn(
                "Failed to send reload completion notice: "
                f"{type(exc).__name__}"
            )


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
        except Exception as exc:
            _log_warn(
                "Failed to parse noticer.toml rooms; using manifest fallback "
                f"({type(exc).__name__})"
            )

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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number is not allowed: {value}")


def _send_fingerprint(path: str, job: _SendJob) -> str:
    digest = hashlib.sha256()
    digest.update(path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(job.room_id).encode("ascii"))
    digest.update(b"\0")
    digest.update(job.message.encode("utf-8"))
    digest.update(b"\0")
    digest.update(job.image_type.encode("ascii"))
    digest.update(b"\0")
    digest.update(b"1" if job.as_sticker else b"0")
    if job.image_bytes is not None:
        digest.update(hashlib.sha256(job.image_bytes).digest())
    return digest.hexdigest()


def _purge_idempotency_locked(now: float) -> None:
    expired = [
        key
        for key, entry in _idempotency_entries.items()
        if entry.expires_at <= now
    ]
    for key in expired:
        _idempotency_entries.pop(key, None)


def _idempotent_enqueue(
    key: str,
    fingerprint: str,
    job: _SendJob,
) -> tuple[_SendJob | None, int, str]:
    now = time.monotonic()
    with _idempotency_lock:
        _purge_idempotency_locked(now)
        existing = _idempotency_entries.get(key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                return None, 409, "idempotency key was already used for another request"
            existing.expires_at = now + IDEMPOTENCY_TTL_SECONDS
            _idempotency_entries.move_to_end(key)
            return existing.job, 0, ""

        enqueued, detail = _enqueue_job(job)
        if not enqueued:
            return None, 503, detail
        while len(_idempotency_entries) >= IDEMPOTENCY_MAX_ENTRIES:
            _idempotency_entries.popitem(last=False)
        _idempotency_entries[key] = _IdempotencyEntry(
            fingerprint=fingerprint,
            job=job,
            expires_at=now + IDEMPOTENCY_TTL_SECONDS,
        )
        return job, 0, ""


def _validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not 1 <= len(value) <= 128:
        raise ValueError("Idempotency-Key must contain 1 to 128 characters")
    if any(ord(char) < 0x21 or ord(char) > 0x7e for char in value):
        raise ValueError("Idempotency-Key must use printable ASCII without spaces")
    return value


# ============================================================
# HTTP Handler
# ============================================================

class _RequestError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _NoticerHandler(BaseHTTPRequestHandler):
    """兼容 legacy API，并提供严格、可鉴权的 v1 webhook API。"""

    _COMMON_FIELDS = {
        "message",
        "image",
        "image_base64",
        "image_type",
        "as_sticker",
    }

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT)

    def handle(self) -> None:
        self.close_connection = True
        self.handle_one_request()

    def do_GET(self) -> None:
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        if path == "/":
            self._handle_root()
        elif path == "/health":
            self._handle_health()
        elif path == "/ready":
            self._handle_ready()
        elif path == "/status":
            if not self._authorize(_auth_token, optional=True):
                return
            self._handle_status()
        else:
            self._send_api_error(404, "not_found", f"not found: {path}")

    def do_POST(self) -> None:
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        if path == "/send":
            self._handle_send("legacy")
        elif path == "/v1/send":
            self._handle_send("strict")
        elif path == "/v1/send/direct":
            self._handle_send("direct")
        else:
            self._send_api_error(404, "not_found", f"not found: {path}")

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del code, message, explain
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send_api_error(405, "method_not_allowed", "method not allowed")

    def _handle_root(self) -> None:
        body = __doc__.encode("utf-8") if __doc__ else b""
        self._send_raw(
            200,
            body,
            "text/plain; charset=utf-8",
        )

    def _handle_health(self) -> None:
        with _client_lock:
            client_ready = _ica_client is not None
        self._send_json(200, {
            "status": "running",
            "client_ready": client_ready,
        })

    def _handle_ready(self) -> None:
        with _client_lock:
            client_ready = _ica_client is not None
        with _send_state_lock:
            queue_ready = _send_accepting and _send_queue is not None
        ready = client_ready and queue_ready
        self._send_json(
            200 if ready else 503,
            {
                "status": "ready" if ready else "not_ready",
                "client_ready": client_ready,
                "queue_ready": queue_ready,
            },
        )

    def _handle_status(self) -> None:
        rooms: dict[str, dict[str, object]] = {}
        for name, room_id in _rooms.items():
            rooms[name] = {
                "room_id": room_id,
                "description": _get_room_description(name),
                "configured": True,
            }
        for name, description in ROOM_DESCRIPTIONS.items():
            if name not in rooms:
                rooms[name] = {
                    "room_id": 0,
                    "description": description,
                    "configured": False,
                }

        with _client_lock:
            client_ready = _ica_client is not None
        with _send_state_lock:
            send_queue = _send_queue
            queue_ready = _send_accepting and send_queue is not None
            queue_depth = send_queue.qsize() if send_queue is not None else 0
        self._send_json(200, {
            "server": f"noticer/{PLUGIN_MANIFEST.version}",
            "status": "running",
            "client_ready": client_ready,
            "rooms": rooms,
            "queue": {
                "ready": queue_ready,
                "depth": queue_depth,
                "capacity": _queue_capacity,
            },
        })

    def _handle_send(self, mode: str) -> None:
        started_at = time.monotonic()
        request_id = uuid.uuid4().hex
        legacy = mode == "legacy"

        if mode == "direct":
            if not _direct_token:
                self._send_api_error(
                    503,
                    "direct_api_disabled",
                    "direct API is disabled",
                    request_id,
                    legacy=False,
                )
                return
            if not self._authorize(_direct_token, optional=False, request_id=request_id):
                return
        elif not self._authorize(
            _auth_token,
            optional=True,
            request_id=request_id,
            legacy=legacy,
        ):
            return

        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._send_api_error(
                415,
                "unsupported_media_type",
                "Content-Type must be application/json",
                request_id,
                legacy=legacy,
            )
            return

        try:
            data = self._read_json_body()
            job, deprecated_direct = self._build_job(data, mode, request_id)
            idempotency_key = (
                _validate_idempotency_key(self.headers.get("Idempotency-Key"))
                if not legacy
                else None
            )
        except _RequestError as exc:
            self._send_api_error(
                exc.status,
                exc.code,
                exc.message,
                request_id,
                legacy=legacy,
            )
            return
        except ValueError as exc:
            self._send_api_error(
                400,
                "invalid_idempotency_key",
                str(exc),
                request_id,
                legacy=legacy,
            )
            return

        with _client_lock:
            client_ready = _ica_client is not None
        if not client_ready:
            self._send_api_error(
                503,
                "client_not_ready",
                "bot client not ready yet",
                request_id,
                legacy=legacy,
            )
            return

        if idempotency_key is None:
            status, detail = _queue_send(job)
        else:
            fingerprint = _send_fingerprint(self.path, job)
            selected_job, error_status, error_detail = _idempotent_enqueue(
                idempotency_key,
                fingerprint,
                job,
            )
            if selected_job is None:
                self._send_api_error(
                    error_status,
                    self._error_code(error_status, error_detail),
                    error_detail,
                    request_id,
                    legacy=False,
                )
                return
            job = selected_job
            request_id = job.request_id
            status, detail = _wait_for_job(job)

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        image_size = len(job.image_bytes) if job.image_bytes is not None else 0
        log_line = (
            f"request_id={request_id} path={self.path!r} "
            f"target={job.room_name!r} text_len={len(job.message)} "
            f"image_bytes={image_size} status={status} elapsed_ms={elapsed_ms}"
        )
        if 200 <= status < 300:
            _log_info(log_line)
        else:
            _log_warn(log_line)

        headers: dict[str, str] = {}
        if deprecated_direct:
            headers = {
                "Deprecation": "true",
                "Link": '</v1/send/direct>; rel="successor-version"',
            }
            _log_warn(
                f"request_id={request_id} legacy /send room_id is deprecated "
                "and will be removed in noticer 0.5"
            )

        if status == 200:
            if legacy:
                self._send_json(
                    200,
                    {"status": "ok", "room": job.room_name},
                    headers=headers,
                )
            else:
                body: dict[str, object] = {
                    "status": "ok",
                    "request_id": request_id,
                }
                if mode == "direct":
                    body["room_id"] = job.room_id
                else:
                    body["room"] = job.room_name
                self._send_json(200, body)
            return

        if status == 503 and detail == "send queue is full":
            headers["Retry-After"] = "1"
        self._send_api_error(
            status,
            self._error_code(status, detail),
            detail,
            request_id,
            legacy=legacy,
            headers=headers,
        )

    def _read_json_body(self) -> dict[str, object]:
        content_len_raw = self.headers.get("Content-Length")
        try:
            content_len = int(content_len_raw or "0")
        except (TypeError, ValueError):
            raise _RequestError(400, "invalid_content_length", "invalid Content-Length")
        if content_len <= 0:
            raise _RequestError(400, "empty_body", "empty body")
        if content_len > MAX_BODY_SIZE:
            raise _RequestError(
                413,
                "payload_too_large",
                f"request body too large (max {MAX_BODY_SIZE} bytes)",
            )
        try:
            raw = self.rfile.read(content_len)
        except (socket.timeout, TimeoutError):
            raise _RequestError(408, "request_timeout", "request body read timeout")
        except OSError:
            raise _RequestError(400, "body_read_failed", "failed to read request body")
        if len(raw) != content_len:
            raise _RequestError(400, "incomplete_body", "incomplete request body")
        try:
            data = json.loads(raw, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _RequestError(400, "invalid_json", f"invalid JSON: {exc}")
        if not isinstance(data, dict):
            raise _RequestError(400, "invalid_request", "JSON body must be an object")
        return cast(dict[str, object], data)

    def _build_job(
        self,
        data: dict[str, object],
        mode: str,
        request_id: str,
    ) -> tuple[_SendJob, bool]:
        legacy = mode == "legacy"
        deprecated_direct = False

        if mode == "strict":
            unknown = set(data) - (self._COMMON_FIELDS | {"room"})
            if unknown:
                names = ", ".join(sorted(unknown))
                raise _RequestError(400, "unknown_field", f"unknown field(s): {names}")
        elif mode == "direct":
            unknown = set(data) - (self._COMMON_FIELDS | {"room_id"})
            if unknown:
                names = ", ".join(sorted(unknown))
                raise _RequestError(400, "unknown_field", f"unknown field(s): {names}")

        room_name_raw = data.get("room")
        room_id_raw = data.get("room_id")

        if mode == "direct":
            room_id = self._require_room_id(room_id_raw)
            room_name = f"room_{room_id}"
        elif legacy and room_id_raw is not None:
            room_id = self._require_room_id(room_id_raw)
            room_name = (
                room_name_raw.strip()
                if isinstance(room_name_raw, str) and room_name_raw.strip()
                else f"room_{room_id}"
            )
            deprecated_direct = True
        else:
            if not isinstance(room_name_raw, str) or not room_name_raw.strip():
                raise _RequestError(
                    400,
                    "room_required",
                    "`room` is required (available rooms: "
                    + _get_room_names_str()
                    + ")",
                )
            room_name = room_name_raw.strip()
            if room_name not in _rooms:
                raise _RequestError(
                    400,
                    "unknown_room",
                    f"unknown room '{room_name}', available: {_get_room_names_str()}",
                )
            room_id = _get_room_id(room_name)
            if room_id == 0:
                raise _RequestError(
                    400,
                    "room_not_configured",
                    f"room '{room_name}' is not configured (room_id = 0)",
                )

        message = data.get("message")
        if message is None:
            message = ""
        if not isinstance(message, str):
            raise _RequestError(400, "invalid_message", "`message` must be a string")

        image_bytes, image_type, as_sticker, image_error = _parse_image_payload(
            data,
            strict=not legacy,
        )
        if image_error is not None:
            raise _RequestError(400, "invalid_image", image_error)
        if message == "" and image_bytes is None:
            raise _RequestError(
                400,
                "content_required",
                "`message` or `image` is required",
            )

        return (
            _SendJob(
                request_id=request_id,
                room_name=room_name,
                room_id=room_id,
                message=message,
                image_bytes=image_bytes,
                image_type=image_type,
                as_sticker=as_sticker,
            ),
            deprecated_direct,
        )

    @staticmethod
    def _require_room_id(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _RequestError(
                400,
                "invalid_room_id",
                "`room_id` must be an integer",
            )
        return value

    def _authorize(
        self,
        expected_token: str,
        *,
        optional: bool,
        request_id: str | None = None,
        legacy: bool = False,
    ) -> bool:
        if optional and not expected_token:
            return True
        authorization = self.headers.get("Authorization", "")
        scheme, separator, supplied = authorization.partition(" ")
        authorized = (
            bool(separator)
            and scheme.lower() == "bearer"
            and bool(supplied)
            and hmac.compare_digest(supplied.strip(), expected_token)
        )
        if authorized:
            return True
        self._send_api_error(
            401,
            "unauthorized",
            "valid Bearer token required",
            request_id,
            legacy=legacy,
            headers={"WWW-Authenticate": "Bearer"},
        )
        return False

    @staticmethod
    def _error_code(status: int, detail: str) -> str:
        if status == 404:
            return "room_not_found"
        if status == 409:
            return "idempotency_conflict"
        if status == 503:
            if "queue" in detail:
                return "queue_unavailable"
            return "client_not_ready"
        if status == 504:
            if "outcome unknown" in detail:
                return "outcome_unknown"
            return "queue_timeout"
        return "send_failed"

    def _send_api_error(
        self,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        *,
        legacy: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        if legacy:
            body: dict[str, object] = {"error": message}
        else:
            body = {
                "status": "error",
                "error": {"code": code, "message": message},
            }
            if request_id is not None:
                body["request_id"] = request_id
        self._send_json(status, body, headers=headers)

    def _send_json(
        self,
        status: int,
        data: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_raw(
            status,
            body,
            "application/json; charset=utf-8",
            headers=headers,
        )

    def _send_raw(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        status_text = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            405: "Method Not Allowed",
            408: "Request Timeout",
            409: "Conflict",
            413: "Payload Too Large",
            415: "Unsupported Media Type",
            500: "Internal Server Error",
            503: "Service Unavailable",
            504: "Gateway Timeout",
        }.get(status, "Unknown")
        response_headers = [
            f"HTTP/1.1 {status} {status_text}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        for name, value in (headers or {}).items():
            response_headers.append(f"{name}: {value}")
        response = ("\r\n".join(response_headers) + "\r\n\r\n").encode("utf-8") + body
        self.close_connection = True
        try:
            self.request.sendall(response)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


# ============================================================
# 插件生命周期
# ============================================================

def on_load() -> None:
    """插件加载时 — 读取配置 + 启动 HTTP server"""
    global _server, _server_thread, _host, _port, _ica_client
    global _auth_token, _direct_token, _queue_capacity, _send_timeout_seconds
    global _retry_attempts, _retry_delay_seconds

    # 读取配置 — [main] 部分
    main_cfg = PLUGIN_MANIFEST.config_unchecked("main")

    raw_host: Any = main_cfg.get_value("host")
    _host = DEFAULT_HOST if raw_host in (None, "") else str(raw_host)

    raw_port: Any = main_cfg.get_value("port")
    try:
        _port = int(raw_port) if raw_port not in (None, "") else DEFAULT_PORT
    except (TypeError, ValueError):
        _port = DEFAULT_PORT
    if not 1 <= _port <= 65535:
        _log_warn(f"Invalid port {_port}; using default {DEFAULT_PORT}")
        _port = DEFAULT_PORT

    raw_auth_token: Any = main_cfg.get_value("auth_token")
    _auth_token = str(raw_auth_token).strip() if raw_auth_token else ""
    raw_direct_token: Any = main_cfg.get_value("direct_token")
    _direct_token = str(raw_direct_token).strip() if raw_direct_token else ""

    raw_queue_capacity: Any = main_cfg.get_value("queue_capacity")
    try:
        _queue_capacity = int(raw_queue_capacity)
    except (TypeError, ValueError):
        _queue_capacity = DEFAULT_QUEUE_CAPACITY
    if not 1 <= _queue_capacity <= 4096:
        _log_warn(
            f"Invalid queue_capacity {_queue_capacity}; "
            f"using default {DEFAULT_QUEUE_CAPACITY}"
        )
        _queue_capacity = DEFAULT_QUEUE_CAPACITY

    raw_send_timeout: Any = main_cfg.get_value("send_timeout_seconds")
    try:
        _send_timeout_seconds = float(raw_send_timeout)
    except (TypeError, ValueError):
        _send_timeout_seconds = DEFAULT_SEND_TIMEOUT_SECONDS
    if not math.isfinite(_send_timeout_seconds) or not 1 <= _send_timeout_seconds <= 300:
        _log_warn(
            f"Invalid send_timeout_seconds {_send_timeout_seconds}; "
            f"using default {DEFAULT_SEND_TIMEOUT_SECONDS}"
        )
        _send_timeout_seconds = DEFAULT_SEND_TIMEOUT_SECONDS

    raw_retry_attempts: Any = main_cfg.get_value("retry_attempts")
    try:
        _retry_attempts = int(raw_retry_attempts)
    except (TypeError, ValueError):
        _retry_attempts = DEFAULT_RETRY_ATTEMPTS
    if not 1 <= _retry_attempts <= 16:
        _log_warn(
            f"Invalid retry_attempts {_retry_attempts}; "
            f"using default {DEFAULT_RETRY_ATTEMPTS}"
        )
        _retry_attempts = DEFAULT_RETRY_ATTEMPTS

    raw_retry_delay: Any = main_cfg.get_value("retry_delay_seconds")
    try:
        _retry_delay_seconds = float(raw_retry_delay)
    except (TypeError, ValueError):
        _retry_delay_seconds = DEFAULT_RETRY_DELAY_SECONDS
    if not math.isfinite(_retry_delay_seconds) or not 0 <= _retry_delay_seconds <= 30:
        _log_warn(
            f"Invalid retry_delay_seconds {_retry_delay_seconds}; "
            f"using default {DEFAULT_RETRY_DELAY_SECONDS}"
        )
        _retry_delay_seconds = DEFAULT_RETRY_DELAY_SECONDS

    # 动态读取所有房间 — [rooms] 部分
    _load_rooms()

    with _client_lock:
        _ica_client = _get_persisted_client()

    # 启动 HTTP server (daemon 线程, 主线程退出时自动结束)
    try:
        with _idempotency_lock:
            _idempotency_entries.clear()
        _start_send_worker()
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
            f"queue_capacity={_queue_capacity} "
            f"send_timeout={_send_timeout_seconds:g}s "
            f"retry={_retry_attempts}x/{_retry_delay_seconds:g}s "
            f"auth={'enabled' if _auth_token else 'disabled'} "
            f"direct={'enabled' if _direct_token else 'disabled'}\n"
            + rooms_str
        )

        with _client_lock:
            client = _ica_client
        if client is not None:
            _maybe_send_reload_notice(client)
    except Exception as exc:
        _stop_send_worker()
        print(f"[noticer] Failed to start HTTP server: {type(exc).__name__}: {exc}")


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
    _stop_send_worker()
    with _idempotency_lock:
        _idempotency_entries.clear()
    print("[noticer] HTTP server stopped")


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    """捕获 client 实例 + 响应 /noticer 命令"""
    global _ica_client

    client_updated = False
    with _client_lock:
        previous_client_id = (
            _ica_client.client_id if _ica_client is not None else None
        )
        client_updated = (
            _ica_client is None
            or previous_client_id != client.client_id
        )
        # bot 可能为每次回调创建新的 Python wrapper。始终刷新引用以免持有
        # 失效对象，但不要把 wrapper 身份变化误报成 client 重连。
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
