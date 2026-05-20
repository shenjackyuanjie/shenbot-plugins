"""
ds_monitor.py - DeepSeek 网页更新监测插件

定期调用 ds-monitor 二进制检查 chat.deepseek.com 页面变更，
检测到变化时自动通过 noticer 发送 AI 分析结果。

配置 (config/ds_monitor.toml):

```toml
[ds_monitor]
binary = "D:\\githubs\\deepseek\\web_craw\\target\\release\\ds-monitor.exe"
config = "D:\\githubs\\deepseek\\web_craw\\config.toml"
interval = 600
```
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

from shenbot_api import PluginManifest, ConfigStorage, Scheduler

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="ds_monitor",
    name="DeepSeek 网页更新监测",
    version="0.1.0",
    description="定期检查 chat.deepseek.com 页面变更，Claude Code 分析后推送通知",
    authors=["shenjack"],
    config={
        "ds_monitor": ConfigStorage(
            binary="D:\\githubs\\deepseek\\web_craw\\target\\release\\ds-monitor.exe",
            config="D:\\githubs\\deepseek\\web_craw\\config.toml",
            interval=600,
        ),
    },
)

_binary_path: str = ""
_config_path: str = ""
_check_interval: int = 600
_enabled: bool = True

_last_check_time: datetime | None = None
_last_change_time: datetime | None = None
_last_change_summary: str = ""

_scheduler: Scheduler | None = None
_client: "IcaClient | None" = None


def _log(msg: str) -> None:
    if _client is not None:
        try:
            _client.info(f"[ds-monitor] {msg}")
            return
        except Exception:
            pass
    print(f"[ds-monitor] {msg}")


def _notify_error(msg: str, room_id: int | None = None) -> None:
    """通过 QQ 报告错误"""
    try:
        if _client is not None:
            _client.warn(msg)
            if room_id is not None:
                import urllib.request, json as _json
                body = _json.dumps({
                    "room_id": room_id,
                    "message": f"[ds-monitor] {msg}",
                }).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:10020/send",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _load_config() -> None:
    global _binary_path, _config_path, _check_interval

    cfg = PLUGIN_MANIFEST.config_unchecked("ds_monitor")

    raw_bin = cfg.get_value("binary")
    _binary_path = str(raw_bin) if raw_bin else ""

    raw_cfg = cfg.get_value("config")
    cfg_str = str(raw_cfg) if raw_cfg else ""
    if cfg_str and not os.path.isabs(cfg_str):
        cfg_str = os.path.abspath(cfg_str)
    _config_path = cfg_str

    raw_int = cfg.get_value("interval")
    try:
        _check_interval = int(raw_int) if raw_int else 600
    except (TypeError, ValueError):
        _check_interval = 600


def _run_binary(room_id: int | None = None) -> str:
    if not _binary_path or not os.path.isfile(_binary_path):
        msg = f"❌ ds-monitor 二进制不存在: {_binary_path}"
        _log(msg)
        _notify_error(msg, room_id)
        return msg

    args = [_binary_path, "--config", _config_path, "check"]
    if room_id is not None:
        args.append(f"--noticer-room-id={room_id}")
        args.append(f"--room=room_{room_id}")

    # ds-monitor resolves relative paths in config.toml (snapshot/settings)
    # from its current working directory. Run it from the config directory so
    # "snapshot.json" and "claude-setting.json" point at web_craw/, not
    # target/release/.
    cwd = os.path.dirname(_config_path) if _config_path else ""
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.dirname(_binary_path) or "."

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            cwd=cwd,
        )
        out = result.stdout
        if result.stderr:
            out += "\n" + result.stderr
        return out
    except subprocess.TimeoutExpired:
        msg = "❌ ds-monitor 检查超时 (300s)"
        _log(msg)
        _notify_error(msg, room_id)
        return msg
    except Exception as e:
        msg = f"❌ ds-monitor 执行失败: {e}"
        _log(msg)
        _notify_error(msg, room_id)
        return msg


def _do_check(room_id: int | None = None) -> None:
    global _last_check_time, _last_change_time, _last_change_summary

    if not _enabled and room_id is None:
        return

    _log("开始检查...")
    output = _run_binary(room_id)
    _last_check_time = datetime.now(timezone.utc)

    if "检测到变化" in output:
        _last_change_time = _last_check_time
        summary_lines: list[str] = []
        in_summary = False
        for line in output.split("\n"):
            if "检测到变化" in line:
                in_summary = True
                continue
            if in_summary:
                if line.startswith("===") or "已发送通知" in line:
                    break
                stripped = line.strip()
                if stripped:
                    summary_lines.append(stripped)
        _last_change_summary = "\n".join(summary_lines) if summary_lines else "检测到页面变更"
        _log(f"检测到变化！{_last_change_summary[:100]}")
    elif "无变化" in output:
        _log("无变化")
    else:
        _log(f"检查结果: {output[:120]}")


def on_load() -> None:
    global _scheduler

    _load_config()
    _log(f"加载完成 (binary={_binary_path}, interval={_check_interval}s)")

    _scheduler = Scheduler(_do_check, timedelta(seconds=_check_interval))
    _scheduler.start()

    # 启动后 10s 做首次检查
    def _first():
        time.sleep(10)
        _do_check()

    threading.Thread(target=_first, daemon=True).start()


def on_unload() -> None:
    _log("卸载")


def on_ica_message(msg: "IcaNewMessage", client: "IcaClient") -> None:
    global _client

    if _client is None or _client.client_id != client.client_id:
        _client = client

    if msg.is_from_self or msg.is_reply:
        return

    content = (msg.content or "").strip()
    if not content:
        return

    if content == "/monitor":
        _cmd_status(msg, client)
    elif content == "/monitor check":
        _cmd_check(msg, client)
    elif content == "/monitor on":
        _cmd_enable(msg, client, True)
    elif content == "/monitor off":
        _cmd_enable(msg, client, False)
    elif content == "/monitor help":
        _cmd_help(msg, client)


def _cmd_status(msg: "IcaNewMessage", client: "IcaClient") -> None:
    lines = [
        "🔍 DeepSeek 网页监测",
        f"状态: {'✅ 运行中' if _enabled else '⏸ 已暂停'}",
        f"检查间隔: {_check_interval}s",
    ]
    if _last_check_time:
        lines.append(
            f"上次检查: {_last_check_time.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
    if _last_change_time:
        lines.append(
            f"上次变更: {_last_change_time.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        if _last_change_summary:
            lines.append(f"变更摘要:\n{_last_change_summary}")

    client.send_message(msg.reply_with("\n".join(lines)))


def _cmd_check(msg: "IcaNewMessage", client: "IcaClient") -> None:
    room_id = int(msg.room_id)
    client.send_message(msg.reply_with("🔍 正在检查..."))
    _do_check(room_id)
    if _last_change_time and _last_check_time and _last_change_time >= _last_check_time:
        return  # 有变更，ds-monitor 已经发了详细通知
    client.send_message(msg.reply_with("✅ 检查完成，无变化"))


def _cmd_enable(msg: "IcaNewMessage", client: "IcaClient", on: bool) -> None:
    global _enabled
    _enabled = on
    client.send_message(msg.reply_with("✅ 监测已开启" if on else "⏸ 监测已暂停"))


def _cmd_help(msg: "IcaNewMessage", client: "IcaClient") -> None:
    client.send_message(
        msg.reply_with(
            "🔍 DeepSeek 网页监测\n"
            "/monitor         - 查看状态\n"
            "/monitor check   - 手动检查\n"
            "/monitor on/off  - 开关\n"
            "/monitor help    - 帮助"
        )
    )
