"""
ds_monitor.py - DeepSeek 网页更新监测插件

启动 ds-monitor 二进制的 watch 模式监控 chat.deepseek.com 页面变更，
检测到变化时由 ds-monitor 自动通过 noticer 发送 AI 分析结果。

配置 (config/ds_monitor.toml):

```toml
[ds_monitor]
binary = "D:\\githubs\\deepseek\\web_craw\\target\\release\\ds-monitor.exe"
config = "D:\\githubs\\deepseek\\web_craw\\config.toml"
interval = 600
```
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

from shenbot_api import PluginManifest, ConfigStorage

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

_client: "IcaClient | None" = None
_watch_process: subprocess.Popen[str] | None = None
_watch_thread: threading.Thread | None = None
_watch_lock = threading.Lock()
_watch_stop_requested: bool = False
_watch_summary_lines: list[str] | None = None
_last_restart_cmd_at: float = 0.0
_last_recent_cmd_at: float = 0.0
_last_analyze_cmd_at: float = 0.0

COMMAND_COOLDOWN_SECS = 60.0


def _ds(msg: str) -> str:
    return f"DS 监测：{msg}"


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
            _client.warn(_ds(msg))
            if room_id is not None:
                import urllib.request, json as _json
                body = _json.dumps({
                    "room_id": room_id,
                    "message": _ds(msg),
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


def _work_dir() -> str:
    # ds-monitor resolves relative paths in config.toml (output/settings)
    # from its current working directory. Run it from the config directory so
    # "output" and "claude-setting.json" point at web_craw/, not target/release/.
    cwd = os.path.dirname(_config_path) if _config_path else ""
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.dirname(_binary_path) or "."
    return cwd


def _output_dir() -> str:
    base = _work_dir()
    output = "output"
    if _config_path and os.path.isfile(_config_path):
        try:
            import tomllib

            with open(_config_path, "rb") as f:
                cfg = tomllib.load(f)
            raw_output = cfg.get("target", {}).get("output")
            if raw_output:
                output = str(raw_output)
        except Exception as e:
            _log(f"读取 output 配置失败，使用默认 output: {e}")

    if not os.path.isabs(output):
        output = os.path.join(base, output)
    return output


def _base_args(command: str) -> list[str]:
    return [_binary_path, "--config", _config_path, command]


def _validate_binary(room_id: int | None = None) -> bool:
    if not _binary_path or not os.path.isfile(_binary_path):
        msg = f"❌ ds-monitor 二进制不存在: {_binary_path}"
        _log(msg)
        _notify_error(msg, room_id)
        return False
    return True


def _run_binary(room_id: int | None = None) -> str:
    if not _validate_binary(room_id):
        return f"❌ ds-monitor 二进制不存在: {_binary_path}"

    args = _base_args("check")
    if room_id is not None:
        args.append(f"--noticer-room-id={room_id}")
        args.append(f"--room=room_{room_id}")

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            cwd=_work_dir(),
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


def _run_last_analyze(room_id: int | None = None) -> str:
    if not _validate_binary(room_id):
        return f"❌ ds-monitor 二进制不存在: {_binary_path}"

    args = _base_args("analyze-last")
    if room_id is not None:
        args.append(f"--noticer-room-id={room_id}")
        args.append(f"--room=room_{room_id}")

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            cwd=_work_dir(),
        )
        out = result.stdout
        if result.stderr:
            out += "\n" + result.stderr
        return out
    except subprocess.TimeoutExpired:
        msg = "❌ ds-monitor 最近变更分析超时 (300s)"
        _log(msg)
        _notify_error(msg, room_id)
        return msg
    except Exception as e:
        msg = f"❌ ds-monitor 最近变更分析失败: {e}"
        _log(msg)
        _notify_error(msg, room_id)
        return msg


def _handle_output_line(line: str) -> None:
    global _last_check_time, _last_change_time, _last_change_summary, _watch_summary_lines

    line = line.rstrip()
    if not line:
        return

    _log(line[:300])

    if "检测到变化" in line:
        now = datetime.now(timezone.utc)
        _last_check_time = now
        _last_change_time = now
        _watch_summary_lines = []
        return

    if "无变化" in line:
        _last_check_time = datetime.now(timezone.utc)
        _watch_summary_lines = None
        return

    if _watch_summary_lines is not None:
        if line.startswith("===") or "已发送通知" in line:
            if _watch_summary_lines:
                _last_change_summary = "\n".join(_watch_summary_lines)
            _watch_summary_lines = None
            return

        stripped = line.strip()
        if stripped:
            _watch_summary_lines.append(stripped)
            if len(_watch_summary_lines) >= 20:
                _last_change_summary = "\n".join(_watch_summary_lines)
                _watch_summary_lines = None


def _watch_stdout_loop(proc: subprocess.Popen[str]) -> None:
    global _watch_process

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            _handle_output_line(line)
    except Exception as e:
        _log(f"watch 输出读取失败: {e}")
    finally:
        code = proc.poll()
        with _watch_lock:
            if _watch_process is proc:
                _watch_process = None
        if not _watch_stop_requested:
            _log(f"watch 进程退出，exit={code}")


def _start_watch() -> bool:
    global _watch_process, _watch_thread, _watch_stop_requested

    with _watch_lock:
        if _watch_process is not None and _watch_process.poll() is None:
            return True

        if not _validate_binary():
            return False

        args = _base_args("watch")
        args.append(f"--interval={_check_interval}")

        _watch_stop_requested = False
        try:
            _watch_process = subprocess.Popen(
                args,
                cwd=_work_dir(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
            )
        except Exception as e:
            _watch_process = None
            _log(f"watch 启动失败: {e}")
            _notify_error(f"❌ ds-monitor watch 启动失败: {e}")
            return False

        _watch_thread = threading.Thread(
            target=_watch_stdout_loop,
            args=(_watch_process,),
            daemon=True,
        )
        _watch_thread.start()
        _log(f"watch 已启动 (pid={_watch_process.pid}, interval={_check_interval}s)")
        return True


def _stop_watch(timeout: float = 10.0) -> None:
    global _watch_process, _watch_stop_requested

    with _watch_lock:
        proc = _watch_process
        _watch_stop_requested = True

    if proc is None or proc.poll() is not None:
        with _watch_lock:
            if _watch_process is proc:
                _watch_process = None
        return

    _log(f"停止 watch 进程 (pid={proc.pid})")
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(f"watch 进程未按时退出，强制结束进程树 (pid={proc.pid})")
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _log(f"watch 进程 kill 后仍未退出 (pid={proc.pid})")

    with _watch_lock:
        if _watch_process is proc:
            _watch_process = None


def _cooling_down(last_at: float) -> bool:
    return time.monotonic() - last_at < COMMAND_COOLDOWN_SECS


def _is_admin(msg: "IcaNewMessage", client: "IcaClient") -> bool:
    return msg.sender_id in client.status.admins


def _count_diff_lines(diff: str) -> tuple[int, int]:
    add = 0
    delete = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            delete += 1
    return add, delete


def _changed_files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line == "=== HTML UNIFIED DIFF ===":
            files.append("HTML")
        elif line.startswith("=== RESOURCE UNIFIED DIFF: "):
            name = line.removeprefix("=== RESOURCE UNIFIED DIFF: ").removesuffix(" ===")
            files.append(name)
    return files[:8]


def _latest_change_report() -> str:
    output_dir = _output_dir()
    if not os.path.isdir(output_dir):
        return _ds(f"还没有历史输出目录: {output_dir}")

    try:
        names = sorted(os.listdir(output_dir), reverse=True)
    except OSError as e:
        return _ds(f"读取历史输出目录失败: {e}")

    for name in names:
        path = os.path.join(output_dir, name)
        if not os.path.isdir(path):
            continue

        diff_path = os.path.join(path, "diff-from-previous.patch")
        if not os.path.isfile(diff_path):
            continue

        metadata: dict[str, Any] = {}
        metadata_path = os.path.join(path, "metadata.json")
        if os.path.isfile(metadata_path):
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception as e:
                _log(f"读取最近变更 metadata 失败: {e}")

        try:
            with open(diff_path, "r", encoding="utf-8", errors="replace") as f:
                diff = f.read()
        except OSError as e:
            return _ds(f"读取最近变更 diff 失败: {e}")

        add, delete = _count_diff_lines(diff)
        changed_files = _changed_files_from_diff(diff)
        lines = [
            "DS 监测：最近一次网页修改",
            f"时间: {metadata.get('timestamp', name)}",
            f"Commit: {metadata.get('commit_id') or 'N/A'}",
            f"SHA256: {str(metadata.get('sha256') or 'N/A')[:12]}",
            f"Diff: +{add}/-{delete} 行",
        ]
        if changed_files:
            lines.append("变更范围:")
            lines.extend(f"  - {file}" for file in changed_files)
        if _last_change_summary:
            lines.append(f"运行摘要:\n{_last_change_summary}")
        lines.append(f"目录: {name}")
        return "\n".join(lines)

    return _ds("还没有记录到带 diff 的历史网页修改")


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
    _load_config()
    _log(f"加载完成 (binary={_binary_path}, interval={_check_interval}s)")
    if _enabled:
        _start_watch()


def on_unload() -> None:
    _stop_watch()
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
    elif content == "/monitor last analyze":
        _cmd_last_analyze(msg, client)
    elif content == "/monitor last":
        _cmd_last(msg, client)
    elif content == "/monitor on":
        _cmd_enable(msg, client, True)
    elif content == "/monitor off":
        _cmd_enable(msg, client, False)
    elif content == "/monitor help":
        _cmd_help(msg, client)


def _cmd_status(msg: "IcaNewMessage", client: "IcaClient") -> None:
    lines = [
        "🔍 DS 监测：DeepSeek 网页监测",
        f"状态: {'✅ 运行中' if _enabled and _watch_process is not None and _watch_process.poll() is None else '⏸ 已暂停'}",
        f"检查间隔: {_check_interval}s",
    ]
    if _watch_process is not None and _watch_process.poll() is None:
        lines.append(f"watch pid: {_watch_process.pid}")
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
    global _enabled, _last_restart_cmd_at

    if _cooling_down(_last_restart_cmd_at):
        return

    _last_restart_cmd_at = time.monotonic()
    _enabled = True
    client.send_message(msg.reply_with(_ds("正在重启 watch，启动后会立即检查")))

    def _restart() -> None:
        _stop_watch()
        ok = _start_watch()
        client.send_message(
            msg.reply_with(_ds("watch 已重启") if ok else _ds("watch 重启失败"))
        )

    threading.Thread(target=_restart, daemon=True).start()


def _cmd_last(msg: "IcaNewMessage", client: "IcaClient") -> None:
    global _last_recent_cmd_at

    if _cooling_down(_last_recent_cmd_at):
        return

    _last_recent_cmd_at = time.monotonic()
    client.send_message(msg.reply_with(_latest_change_report()))


def _cmd_last_analyze(msg: "IcaNewMessage", client: "IcaClient") -> None:
    global _last_analyze_cmd_at

    report = _latest_change_report()
    if not _is_admin(msg, client):
        client.send_message(
            msg.reply_with(
                report + "\n\n只有管理员才能触发 Claude code分析"
            )
        )
        return

    if _cooling_down(_last_analyze_cmd_at):
        return

    _last_analyze_cmd_at = time.monotonic()
    room_id = int(msg.room_id)
    client.send_message(msg.reply_with(_ds("正在触发最近一次变更的 Claude Code 分析")))

    def _analyze() -> None:
        output = _run_last_analyze(room_id)
        _log(f"最近变更分析结果: {output[:500]}")
        if "没有找到带 diff 的历史变更" in output:
            client.send_message(msg.reply_with(_ds("没有找到带 diff 的历史变更")))
        elif "分析失败" in output or "❌" in output:
            client.send_message(msg.reply_with(_ds(f"最近变更分析失败:\n{output[:800]}")))
        elif "已发送最近一次变更分析" in output:
            client.send_message(
                msg.reply_with(
                    _latest_change_report()
                    + "\n\n"
                    + _ds("最近一次变更的 Claude Code 分析已发送")
                )
            )

    threading.Thread(target=_analyze, daemon=True).start()


def _cmd_enable(msg: "IcaNewMessage", client: "IcaClient", on: bool) -> None:
    global _enabled
    _enabled = on
    if on:
        ok = _start_watch()
        client.send_message(msg.reply_with(_ds("监测已开启") if ok else _ds("监测启动失败")))
    else:
        _stop_watch()
        client.send_message(msg.reply_with(_ds("监测已暂停")))


def _cmd_help(msg: "IcaNewMessage", client: "IcaClient") -> None:
    client.send_message(
        msg.reply_with(
            "🔍 DS 监测：DeepSeek 网页监测\n"
            "/monitor         - 查看状态\n"
            "/monitor check   - 重启 watch 并触发检查\n"
            "/monitor last    - 查看最近一次网页修改\n"
            "/monitor last analyze - 管理员重新分析最近一次修改\n"
            "/monitor on/off  - 开关\n"
            "/monitor help    - 帮助"
        )
    )
