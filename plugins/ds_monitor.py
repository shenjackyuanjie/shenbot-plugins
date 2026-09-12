"""
ds_monitor.py - DeepSeek 网页更新监测插件

启动 ds-monitor 二进制的 watch 模式监控 Chat、Platform 和 API Docs 页面变更，
检测到变化时由 ds-monitor 自动通过 noticer 发送 AI 分析结果。
配置房间通知走 /v1/send；命令触发的动态 room_id 通知走受 Token 保护的 direct API。

`/monitor check` 会重启 watch 并等待本轮的 `=== 本轮检查结果 ... ===` 标记
（ds-monitor 0.2.8 起输出），再回报各目标状态；本轮没有变化时回报“无事发生”。

配置 (config/ds_monitor.toml):

```toml
[ds_monitor]
binary = "D:\\githubs\\deepseek\\web_craw\\target\\release\\ds-monitor.exe"
config = "D:\\githubs\\deepseek\\web_craw\\config.toml"
interval = 600
```

`web_craw/config.toml` 保存 noticer strict/direct URL 与本地 Token；
仓库只提交不含密钥的 `config.example.toml`。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

from shenbot_api import PluginManifest, ConfigStorage

# 分析所用模型名，统一从这里取，避免各处文案写成不同版本
ANALYZE_MODEL = "V41F"

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="ds_monitor",
    name="DeepSeek 网页更新监测",
    version="0.3.7",
    description=f"定期检查 DeepSeek Chat、Platform 和 API Docs 变更，DeepSeek {ANALYZE_MODEL} 分析后推送通知",
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
_target_configs: dict[str, "MonitorTarget"] = {}
_fingerprint_history_path: str = ""

_last_check_time: datetime | None = None
_last_change_time: datetime | None = None
_last_change_summary: str = ""
_last_check_times: dict[str, datetime] = {}
_last_change_times: dict[str, datetime] = {}
_last_change_summaries: dict[str, str] = {}
_active_output_target: str | None = None

_client: "IcaClient | None" = None
_watch_process: subprocess.Popen[str] | None = None
_watch_thread: threading.Thread | None = None
_watch_lock = threading.Lock()
_watch_stop_requested: bool = False
_watch_summary_lines: list[str] | None = None
# watch 进程代次：重启后加一，用于丢弃旧进程残留的输出行
_watch_generation: int = 0
_last_restart_cmd_at: float = 0.0
_last_recent_cmd_at: float = 0.0
_last_analyze_cmd_at: float = 0.0
_last_render_cmd_at: float = 0.0

COMMAND_COOLDOWN_SECS = 60.0

# ds-monitor 在检查阶段结束时输出的机器可读标记
# 形如：=== 本轮检查结果 changes=0 notices=0 errors=0 chat=nochange ... ===
CYCLE_RESULT_MARKER = "本轮检查结果"
# 等待本轮检查结果的最长时间（秒）：抓取 + 指纹探测通常远快于此
CYCLE_WAIT_SECS = 300.0

CYCLE_TARGET_LABELS = {
    "chat": "Chat",
    "platform": "Platform",
    "docs": "API Docs",
    "fingerprint": "系统指纹",
}
CYCLE_STATUS_LABELS = {
    "nochange": "无变化",
    "baseline": "首次抓取（已建基线）",
    "change": "检测到变化",
    "alert": "有告警",
    "error": "检查失败",
    "skipped": "本轮跳过",
}
PAGE_TARGET_KEYS = ("chat", "platform", "docs")

_cycle_event = threading.Event()
_cycle_result_generation: int = -1
_cycle_statuses: dict[str, str] = {}
_cycle_counts: dict[str, int] = {}


@dataclass(frozen=True)
class MonitorTarget:
    key: str
    label: str
    url: str
    output: str
    enabled: bool = True


def ds(msg: str) -> str:
    return f"DS 监测：{msg}"


def log(msg: str) -> None:
    if _client is not None:
        try:
            _client.info(f"[ds-monitor] {msg}")
            return
        except Exception:
            pass
    print(f"[ds-monitor] {msg}")


def notify_error(msg: str, room_id: int | None = None) -> None:
    """通过 QQ 报告错误"""
    try:
        if _client is not None:
            _client.warn(ds(msg))
            if room_id is not None:
                target_room = next(
                    (
                        room
                        for room in _client.status.rooms
                        if int(room.room_id) == room_id
                    ),
                    None,
                )
                if target_room is None:
                    log(f"错误通知目标会话不存在: {room_id}")
                    return
                if not _client.send_message(target_room.new_message_to(ds(msg))):
                    log(f"错误通知发送失败: {room_id}")
    except Exception as exc:
        log(f"错误通知发送异常: {type(exc).__name__}")


def load_config() -> None:
    global _binary_path, _config_path, _check_interval, _target_configs
    global _fingerprint_history_path

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
        _check_interval = int(str(raw_int)) if raw_int else 600
    except (TypeError, ValueError):
        _check_interval = 600

    _target_configs = load_target_configs()
    _fingerprint_history_path = load_fingerprint_history_path()


def work_dir() -> str:
    # ds-monitor resolves relative paths in config.toml (output/settings)
    # from its current working directory. Run it from the config directory so
    # "output" and "claude-setting.json" point at web_craw/, not target/release/.
    cwd = os.path.dirname(_config_path) if _config_path else ""
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.dirname(_binary_path) or "."
    return cwd


def load_target_configs() -> dict[str, MonitorTarget]:
    """读取 ds-monitor 的三类监测目标，缺省值与 Rust 配置保持一致。"""
    raw: dict[str, Any] = {}
    if _config_path and os.path.isfile(_config_path):
        try:
            import tomllib

            with open(_config_path, "rb") as f:
                raw = tomllib.load(f)
        except Exception as exc:
            log(f"读取监测目标配置失败，使用默认目标: {exc}")

    sections = {
        "chat": ("Chat", raw.get("target", {}), True, "https://chat.deepseek.com/", "output/chat"),
        "platform": (
            "Platform",
            raw.get("platform", {}),
            True,
            "https://platform.deepseek.com/",
            "output/platform",
        ),
        "docs": (
            "API Docs",
            raw.get("docs", {}),
            True,
            "https://api-docs.deepseek.com/zh-cn/",
            "output/docs",
        ),
    }
    targets: dict[str, MonitorTarget] = {}
    for key, (label, section, default_enabled, default_url, default_output) in sections.items():
        section = section if isinstance(section, dict) else {}
        enabled = bool(section.get("enabled", default_enabled))
        url = str(section.get("url", section.get("base_url", default_url)))
        output = str(section.get("output", default_output))
        if not os.path.isabs(output):
            output = os.path.join(work_dir(), output)
        targets[key] = MonitorTarget(key, label, url, output, enabled)
    return targets


def load_fingerprint_history_path() -> str:
    """Read the Rust fingerprint history path, using the same default."""
    raw: dict[str, Any] = {}
    if _config_path and os.path.isfile(_config_path):
        try:
            import tomllib

            with open(_config_path, "rb") as f:
                raw = tomllib.load(f)
        except Exception as exc:
            log(f"Failed to read fingerprint config; using default history path: {exc}")

    section = raw.get("fingerprint", {})
    section = section if isinstance(section, dict) else {}
    history = str(section.get("history", "output/fingerprint-history.jsonl"))
    if not os.path.isabs(history):
        history = os.path.join(work_dir(), history)
    return history


def output_dir() -> str:
    target = _target_configs.get("chat")
    if target is not None:
        return target.output
    return os.path.join(work_dir(), "output/chat")


def base_args(command: str) -> list[str]:
    return [_binary_path, "--config", _config_path, command]


def binary_version() -> str:
    if not _binary_path:
        return "未配置"
    if not os.path.isfile(_binary_path):
        return f"不可用（二进制不存在: {_binary_path}）"

    try:
        result = subprocess.run(
            [_binary_path, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_dir(),
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "获取失败（--version 超时）"
    except OSError as exc:
        return f"获取失败（{exc}）"

    output = result.stdout or result.stderr
    version = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
    if result.returncode != 0:
        detail = version or f"退出码 {result.returncode}"
        return f"获取失败（{detail}）"
    return version or "获取失败（--version 无输出）"


def validate_binary(room_id: int | None = None) -> bool:
    if not _binary_path or not os.path.isfile(_binary_path):
        msg = f"❌ ds-monitor 二进制不存在: {_binary_path}"
        log(msg)
        notify_error(msg, room_id)
        return False
    return True


def run_binary(room_id: int | None = None) -> str:
    if not validate_binary(room_id):
        return f"❌ ds-monitor 二进制不存在: {_binary_path}"

    args = base_args("check")
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
            cwd=work_dir(),
        )
        out = result.stdout
        if result.stderr:
            out += "\n" + result.stderr
        return out
    except subprocess.TimeoutExpired:
        msg = "❌ ds-monitor 检查超时（由外部进程终止）"
        log(msg)
        notify_error(msg, room_id)
        return msg
    except Exception as e:
        msg = f"❌ ds-monitor 执行失败: {e}"
        log(msg)
        notify_error(msg, room_id)
        return msg


def run_last_analyze(target: str, room_id: int | None = None) -> str:
    if not validate_binary(room_id):
        return f"❌ ds-monitor 二进制不存在: {_binary_path}"

    args = base_args("analyze-last")
    args.append(f"--target={target}")
    args.append("--reanalyze")
    args.append("--unlimited-timeout")
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
            cwd=work_dir(),
        )
        out = result.stdout
        if result.stderr:
            out += "\n" + result.stderr
        return out
    except subprocess.TimeoutExpired:
        msg = "❌ ds-monitor 最近变更分析超时（由外部进程终止）"
        log(msg)
        notify_error(msg, room_id)
        return msg
    except Exception as e:
        msg = f"❌ ds-monitor 最近变更分析失败: {e}"
        log(msg)
        notify_error(msg, room_id)
        return msg


def run_last_render(target: str | None, room_id: int | None = None) -> str:
    if not validate_binary(room_id):
        return f"❌ ds-monitor 二进制不存在: {_binary_path}"

    args = base_args("render-last")
    if target is not None:
        args.append(f"--target={target}")
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
            cwd=work_dir(),
        )
        out = result.stdout
        if result.stderr:
            out += "\n" + result.stderr
        return out
    except subprocess.TimeoutExpired:
        msg = "❌ ds-monitor 最近分析渲染超时（由外部进程终止）"
        log(msg)
        notify_error(msg, room_id)
        return msg
    except Exception as e:
        msg = f"❌ ds-monitor 最近分析渲染失败: {e}"
        log(msg)
        notify_error(msg, room_id)
        return msg


def handle_output_line(line: str, generation: int) -> None:
    global _last_check_time, _last_change_time, _last_change_summary
    global _watch_summary_lines, _active_output_target

    if generation != _watch_generation:
        # 旧 watch 进程退出前残留的输出，忽略
        return

    line = line.rstrip()
    if not line:
        return

    log(line[:300])

    payload = line
    for key, target in _target_configs.items():
        prefix = f"[{target.label}]"
        if line.startswith(prefix):
            _active_output_target = key
            payload = line[len(prefix) :].lstrip()
            break

    target_key = _active_output_target or "chat"
    now = datetime.now(timezone.utc)

    if payload.startswith("===") and CYCLE_RESULT_MARKER in payload:
        # 记录本轮检查结果；继续走下面的汇总逻辑，让 "===" 正常结束变更摘要
        record_cycle_result(payload, generation)

    if "检测到变化" in payload:
        _last_check_time = now
        _last_change_time = now
        _last_check_times[target_key] = now
        _last_change_times[target_key] = now
        _watch_summary_lines = []
        return

    if "无变化" in payload or "首次抓取" in payload:
        _last_check_time = now
        _last_check_times[target_key] = now
        _watch_summary_lines = None
        return

    if _watch_summary_lines is not None:
        if payload.startswith("===") or "已发送通知" in payload:
            if _watch_summary_lines:
                summary = "\n".join(_watch_summary_lines)
                _last_change_summary = summary
                _last_change_summaries[target_key] = summary
            _watch_summary_lines = None
            return

        stripped = payload.strip()
        if stripped:
            _watch_summary_lines.append(stripped)
            if len(_watch_summary_lines) >= 20:
                summary = "\n".join(_watch_summary_lines)
                _last_change_summary = summary
                _last_change_summaries[target_key] = summary
                _watch_summary_lines = None


def cycle_target_label(key: str) -> str:
    return CYCLE_TARGET_LABELS.get(key, key)


def cycle_status_text(key: str, status: str) -> str:
    return f"{cycle_target_label(key)} {CYCLE_STATUS_LABELS.get(status, status)}"


def record_cycle_result(line: str, generation: int) -> None:
    """解析 `=== 本轮检查结果 ... ===` 标记，并唤醒等待本轮结果的调用方。"""
    global _cycle_result_generation, _cycle_statuses, _cycle_counts

    fields: dict[str, str] = {}
    for token in line.split():
        key, sep, value = token.partition("=")
        # 标记两端的 "===" 会被切成空 key，这里只收 "key=value" 形式
        if not sep or not key:
            continue
        fields[key] = value

    counts: dict[str, int] = {}
    for key in ("changes", "notices", "errors"):
        raw = fields.pop(key, None)
        if raw is not None and raw.lstrip("-").isdigit():
            counts[key] = int(raw)

    _cycle_statuses = fields
    _cycle_counts = counts
    _cycle_result_generation = generation
    _cycle_event.set()


def wait_cycle_result(generation: int, timeout: float) -> bool:
    """等待指定 watch 代次的本轮检查结果。

    超时，或这次检查已被更晚的 /monitor check 取代时返回 False。
    """
    deadline = time.monotonic() + timeout
    while generation == _watch_generation:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if _cycle_event.wait(min(remaining, 1.0)):
            _cycle_event.clear()
            if _cycle_result_generation == generation:
                return True
    return False


def cycle_report(elapsed: float) -> str:
    """把本轮检查结果整理成一条 QQ 消息。"""
    statuses = dict(_cycle_statuses)
    changes = [key for key, status in statuses.items() if status == "change"]
    alerts = [key for key, status in statuses.items() if status == "alert"]
    page_errors = [key for key in PAGE_TARGET_KEYS if statuses.get(key) == "error"]

    if changes:
        headline = "🔔 检测到变化：" + "、".join(cycle_target_label(key) for key in changes)
    elif alerts:
        headline = "⚠️ 本轮有告警：" + "、".join(cycle_target_label(key) for key in alerts)
    elif page_errors:
        headline = (
            "⚠️ 本轮没有变化，但 "
            + "、".join(cycle_target_label(key) for key in page_errors)
            + " 检查失败"
        )
    else:
        headline = "✅ 无事发生"

    lines = [f"{headline}（用时 {elapsed:.0f}s）"]
    detail = " · ".join(
        cycle_status_text(key, status) for key, status in statuses.items()
    )
    if detail:
        lines.append(detail)
    if changes:
        lines.append(f"变更摘要与 DeepSeek {ANALYZE_MODEL} 分析稍后由监测推送")
    return ds("\n".join(lines))


def watch_stdout_loop(proc: subprocess.Popen[str], generation: int) -> None:
    global _watch_process

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            handle_output_line(line, generation)
    except Exception as e:
        log(f"watch 输出读取失败: {e}")
    finally:
        code = proc.poll()
        with _watch_lock:
            if _watch_process is proc:
                _watch_process = None
        if not _watch_stop_requested:
            log(f"watch 进程退出，exit={code}")


def start_watch() -> bool:
    global _watch_process, _watch_thread, _watch_stop_requested, _watch_generation

    with _watch_lock:
        if _watch_process is not None and _watch_process.poll() is None:
            return True

        if not validate_binary():
            return False

        args = base_args("watch")
        args.append(f"--interval={_check_interval}")

        _watch_stop_requested = False
        try:
            _watch_process = subprocess.Popen(
                args,
                cwd=work_dir(),
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
            log(f"watch 启动失败: {e}")
            notify_error(f"❌ ds-monitor watch 启动失败: {e}")
            return False

        _watch_generation += 1
        generation = _watch_generation
        _watch_thread = threading.Thread(
            target=watch_stdout_loop,
            args=(_watch_process, generation),
            daemon=True,
        )
        _watch_thread.start()
        log(f"watch 已启动 (pid={_watch_process.pid}, interval={_check_interval}s, gen={generation})")
        return True


def stop_watch(timeout: float = 10.0) -> None:
    global _watch_process, _watch_stop_requested

    with _watch_lock:
        proc = _watch_process
        _watch_stop_requested = True

    if proc is None or proc.poll() is not None:
        with _watch_lock:
            if _watch_process is proc:
                _watch_process = None
        return

    log(f"停止 watch 进程 (pid={proc.pid})")
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"watch 进程未按时退出，强制结束进程树 (pid={proc.pid})")
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
            log(f"watch 进程 kill 后仍未退出 (pid={proc.pid})")

    with _watch_lock:
        if _watch_process is proc:
            _watch_process = None


def cooling_down(last_at: float) -> bool:
    return time.monotonic() - last_at < COMMAND_COOLDOWN_SECS


def is_admin(msg: "IcaNewMessage", client: "IcaClient") -> bool:
    return msg.sender_id in client.status.admins


def count_diff_lines(diff: str) -> tuple[int, int]:
    add = 0
    delete = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            delete += 1
    return add, delete


def changed_files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line == "=== HTML UNIFIED DIFF ===":
            files.append("HTML")
        elif line.startswith("=== RESOURCE UNIFIED DIFF: "):
            name = line.removeprefix("=== RESOURCE UNIFIED DIFF: ").removesuffix(" ===")
            files.append(name)
    return files[:8]


def latest_web_change_report(target: MonitorTarget) -> str:
    if not os.path.isdir(target.output):
        return f"{target.label}: 还没有历史输出目录 ({target.output})"

    try:
        names = sorted(os.listdir(target.output), reverse=True)
    except OSError as exc:
        return f"{target.label}: 读取历史输出目录失败: {exc}"

    for name in names:
        path = os.path.join(target.output, name)
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
            except Exception as exc:
                log(f"读取 {target.label} 最近变更 metadata 失败: {exc}")

        try:
            with open(diff_path, "r", encoding="utf-8", errors="replace") as f:
                diff = f.read()
        except OSError as exc:
            return f"{target.label}: 读取最近变更 diff 失败: {exc}"

        add, delete = count_diff_lines(diff)
        changed_files = changed_files_from_diff(diff)
        lines = [
            f"{target.label} 最近一次网页修改",
            f"时间: {metadata.get('timestamp', name)}",
            f"Commit: {metadata.get('commit_id') or 'N/A'}",
            f"SHA256: {str(metadata.get('sha256') or 'N/A')[:12]}",
            f"Diff: +{add}/-{delete} 行",
        ]
        if changed_files:
            lines.append("变更范围:")
            lines.extend(f"  - {file}" for file in changed_files)
        if target.key in _last_change_summaries:
            lines.append(f"运行摘要:\n{_last_change_summaries[target.key]}")
        lines.append(f"目录: {name}")
        return "\n".join(lines)

    return f"{target.label}: 还没有记录到带 diff 的历史网页修改"


def latest_docs_change_report(target: MonitorTarget) -> str:
    changes_dir = os.path.join(target.output, "changes")
    try:
        names = sorted(os.listdir(changes_dir), reverse=True)
    except FileNotFoundError:
        return f"{target.label}: 还没有历史变更 manifest"
    except OSError as exc:
        return f"{target.label}: 读取变更目录失败: {exc}"

    for name in names:
        manifest_path = os.path.join(changes_dir, name, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return f"{target.label}: 读取 manifest 失败: {exc}"

        entries = manifest.get("changes", [])
        counts = {kind: 0 for kind in ("added", "updated", "removed")}
        for entry in entries:
            kind = str(entry.get("kind", "")).lower()
            if kind in counts:
                counts[kind] += 1
        lines = [
            f"{target.label} 最近一次网页修改",
            f"时间: {manifest.get('timestamp', name)}",
            f"页面: {len(entries)}（新增 {counts['added']} · 更新 {counts['updated']} · 删除 {counts['removed']}）",
        ]
        for entry in entries[:8]:
            lines.append(
                f"  - [{entry.get('kind', 'unknown')}] {entry.get('title') or entry.get('url', 'N/A')}"
            )
        lines.append(f"清单: {os.path.relpath(manifest_path, target.output)}")
        return "\n".join(lines)
    return f"{target.label}: 还没有历史变更 manifest"


def latest_change_report(target_key: str | None = None) -> str:
    targets = _target_configs
    if target_key is not None:
        target = targets.get(target_key)
        if target is None:
            return ds(f"未知监测目标: {target_key}（可选 chat、platform、docs）")
        if not target.enabled:
            return ds(f"{target.label} 监测未启用")
        reports = [
            latest_docs_change_report(target)
            if target.key == "docs"
            else latest_web_change_report(target)
        ]
    else:
        reports = []
        for key in ("chat", "platform", "docs"):
            target = targets.get(key)
            if target is not None and target.enabled:
                reports.append(
                    latest_docs_change_report(target)
                    if target.key == "docs"
                    else latest_web_change_report(target)
                )
    if not reports:
        return ds("没有启用任何监测目标")
    return ds("最近一次网页修改汇总\n\n" + "\n\n".join(reports))


def fingerprint_history_report(limit: int = 5) -> str:
    if not _fingerprint_history_path:
        return ds("未配置 fingerprint 历史文件")
    if not os.path.isfile(_fingerprint_history_path):
        return ds(f"还没有 fingerprint 历史记录（文件不存在：{_fingerprint_history_path}）")

    try:
        with open(_fingerprint_history_path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError as exc:
        return ds(f"读取 fingerprint 历史失败：{exc}")

    records: list[dict[str, Any]] = []
    for line in reversed(raw_lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("event") not in {"baseline", "change"}:
            continue
        if not isinstance(record.get("checked_at"), str):
            continue
        if not isinstance(record.get("changes"), list):
            continue
        if not isinstance(record.get("models"), dict):
            continue
        if not isinstance(record.get("errors"), list):
            continue
        records.append(record)
        if len(records) >= limit:
            break

    if not records:
        return ds(f"没有有效的 fingerprint 历史记录：{_fingerprint_history_path}")

    lines = [f"DeepSeek 指纹历史（最近 {len(records)} 条）"]
    shown_errors: list[str] | None = None
    for record in records:
        event = record["event"]
        lines.extend(["", f"时间: {record['checked_at']}", f"事件: {'基线' if event == 'baseline' else '变化'}"])

        changes = record["changes"]
        if changes:
            lines.append("指纹变化:")
            for change in changes:
                if not isinstance(change, dict):
                    continue
                old = change.get("old")
                old_text = "无此前记录" if old is None else str(old)
                lines.append(
                    f"  - {change.get('model', 'N/A')}: {old_text} → {change.get('new', 'N/A')}"
                )
        else:
            lines.append("指纹变化：无")

        lines.append("模型快照:")
        models = record["models"]
        if not models:
            lines.append("  - 无已知模型")
        for model_key, snapshot in sorted(models.items()):
            if not isinstance(snapshot, dict):
                lines.append(f"  - {model_key}: {snapshot}")
                continue
            lines.append(
                "  - "
                f"{model_key}: 指纹={snapshot.get('fingerprint', 'N/A')}, "
                f"模型={snapshot.get('model', 'N/A')}, "
                f"缓存命中={snapshot.get('cached_tokens', 'N/A')}"
            )

        errors = record["errors"]
        if errors:
            if shown_errors is not None and sorted(errors) == sorted(shown_errors):
                lines.append(f"错误: 与上一条相同（{len(errors)} 项）")
            else:
                lines.append("错误:")
                lines.extend(f"  - {error}" for error in errors)
        # 记录本条展示（或未展示）的错误，供下一条判断能否折叠
        shown_errors = errors or None

    lines.append(f"\n文件: {_fingerprint_history_path}")
    return ds("\n".join(lines))


def do_check(room_id: int | None = None) -> None:
    global _last_check_time, _last_change_time, _last_change_summary

    if not _enabled and room_id is None:
        return

    log("开始检查...")
    output = run_binary(room_id)
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
        log(f"检测到变化！{_last_change_summary[:100]}")
    elif "无变化" in output:
        log("无变化")
    else:
        log(f"检查结果: {output[:120]}")


def on_load() -> None:
    load_config()
    enabled = ", ".join(
        target.label for target in _target_configs.values() if target.enabled
    )
    log(f"加载完成 (binary={_binary_path}, interval={_check_interval}s, targets={enabled})")
    if _enabled:
        start_watch()


def on_unload() -> None:
    stop_watch()
    log("卸载")


def on_ica_message(msg: "IcaNewMessage", client: "IcaClient") -> None:
    global _client

    if _client is None or _client.client_id != client.client_id:
        _client = client

    if msg.is_from_self or msg.is_reply:
        return

    content = (msg.content or "").strip()
    if not content:
        return

    parts = content.split()
    if content == "/monitor":
        cmd_status(msg, client)
    elif content == "/monitor check":
        cmd_check(msg, client)
    elif content == "/monitor fp":
        cmd_fingerprint(msg, client)
    elif len(parts) == 3 and parts[:2] == ["/monitor", "fp"]:
        cmd_fingerprint(msg, client, parts[2])
    elif len(parts) == 3 and parts[:2] == ["/monitor", "last"]:
        cmd_last(msg, client, parts[2])
    elif content == "/monitor last":
        cmd_last(msg, client)
    elif len(parts) == 4 and parts[:3] == ["/monitor", "render", "last"]:
        cmd_render_last(msg, client, parts[3])
    elif content == "/monitor render last":
        cmd_render_last(msg, client)
    elif len(parts) == 4 and parts[:3] == ["/monitor", "analyze", "last"]:
        cmd_last_analyze(msg, client, parts[3])
    elif content == "/monitor on":
        cmd_enable(msg, client, True)
    elif content == "/monitor off":
        cmd_enable(msg, client, False)
    elif content == "/monitor help":
        cmd_help(msg, client)


def cmd_status(msg: "IcaNewMessage", client: "IcaClient") -> None:
    lines = [
        "🔍 DS 监测：DeepSeek 网页监测",
        f"插件版本: {PLUGIN_MANIFEST.version}",
        f"EXE版本: {binary_version()}",
        f"状态: {'✅ 运行中' if _enabled and _watch_process is not None and _watch_process.poll() is None else '⏸ 已暂停'}",
        f"检查间隔: {_check_interval}s",
    ]
    if _watch_process is not None and _watch_process.poll() is None:
        lines.append(f"watch pid: {_watch_process.pid}")
    if _cycle_statuses:
        last_cycle = "上一轮检查: " + " · ".join(
            cycle_status_text(key, status) for key, status in _cycle_statuses.items()
        )
        failed = _cycle_counts.get("errors", 0)
        if failed:
            last_cycle += f"（失败 {failed} 项）"
        lines.append(last_cycle)
    for key in ("chat", "platform", "docs"):
        target = _target_configs.get(key)
        if target is None:
            continue
        state = "✅ 启用" if target.enabled else "⏸ 未启用"
        lines.append(f"{target.label}: {state}")
        lines.append(f"  输出: {target.output}")
        checked = _last_check_times.get(key)
        changed = _last_change_times.get(key)
        if checked:
            lines.append(f"  上次检查: {checked.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        if changed:
            lines.append(f"  上次变更: {changed.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        summary = _last_change_summaries.get(key)
        if summary:
            lines.append(f"  变更摘要: {summary[:300]}")

    client.send_message(msg.reply_with("\n".join(lines)))


def cmd_check(msg: "IcaNewMessage", client: "IcaClient") -> None:
    global _enabled, _last_restart_cmd_at

    if cooling_down(_last_restart_cmd_at):
        remain = int(COMMAND_COOLDOWN_SECS - (time.monotonic() - _last_restart_cmd_at)) + 1
        client.send_message(msg.reply_with(ds(f"命令冷却中，请 {remain} 秒后再试")))
        return

    _last_restart_cmd_at = time.monotonic()
    _enabled = True
    client.send_message(msg.reply_with(ds("正在重启 watch，启动后会立即检查并回报本轮结果")))

    def restart() -> None:
        stop_watch()
        _cycle_event.clear()
        if not start_watch():
            client.send_message(msg.reply_with(ds("watch 重启失败")))
            return

        generation = _watch_generation
        started = time.monotonic()
        if not wait_cycle_result(generation, CYCLE_WAIT_SECS):
            if generation != _watch_generation:
                # 已被更晚的 /monitor check 取代，让那次检查回报结果
                return
            client.send_message(
                msg.reply_with(
                    ds(
                        f"watch 已重启，但 {int(CYCLE_WAIT_SECS)}s 内没等到本轮检查结果，"
                        "请稍后用 /monitor 查看状态与日志"
                    )
                )
            )
            return

        client.send_message(msg.reply_with(cycle_report(time.monotonic() - started)))

    threading.Thread(target=restart, daemon=True).start()


def cmd_fingerprint(
    msg: "IcaNewMessage", client: "IcaClient", raw_limit: str | None = None
) -> None:
    if raw_limit is None:
        limit = 5
    else:
        try:
            limit = int(raw_limit)
        except ValueError:
            client.send_message(msg.reply_with(ds("用法: /monitor fp [N]，N 必须是 1-20 的整数")))
            return
        if not 1 <= limit <= 20:
            client.send_message(msg.reply_with(ds("N 必须是 1-20 的整数")))
            return

    client.send_message(msg.reply_with(fingerprint_history_report(limit)))


def cmd_last(
    msg: "IcaNewMessage", client: "IcaClient", target_key: str | None = None
) -> None:
    global _last_recent_cmd_at

    if cooling_down(_last_recent_cmd_at):
        return

    _last_recent_cmd_at = time.monotonic()
    client.send_message(msg.reply_with(latest_change_report(target_key)))


def cmd_last_analyze(
    msg: "IcaNewMessage", client: "IcaClient", target_key: str
) -> None:
    global _last_analyze_cmd_at

    target = _target_configs.get(target_key)
    if target is None or not target.enabled:
        client.send_message(msg.reply_with(ds(f"未知或未启用监测目标: {target_key}（可选 chat、platform、docs）")))
        return

    report = latest_change_report(target_key)
    if not is_admin(msg, client):
        client.send_message(
            msg.reply_with(
                report + f"\n\n只有管理员才能触发 DeepSeek {ANALYZE_MODEL} 分析"
            )
        )
        return

    if cooling_down(_last_analyze_cmd_at):
        return

    _last_analyze_cmd_at = time.monotonic()
    room_id = int(msg.room_id)
    client.send_message(
        msg.reply_with(ds(f"正在触发 {target.label} 最近一次变更的 DeepSeek {ANALYZE_MODEL} 分析（无限时长）"))
    )

    def analyze() -> None:
        output = run_last_analyze(target_key, room_id)
        log(f"最近变更分析结果: {output[:500]}")
        if "没有找到带 diff 的历史变更" in output or "没有找到历史变更 manifest" in output:
            client.send_message(msg.reply_with(ds(f"没有找到 {target.label} 的历史变更")))
        elif "分析失败" in output or "❌" in output:
            client.send_message(msg.reply_with(ds(f"{target.label} 最近变更分析失败:\n{output[:800]}")))
        elif "已发送" in output and "分析" in output:
            client.send_message(
                msg.reply_with(
                    latest_change_report(target_key)
                    + "\n\n"
                    + ds(f"{target.label} 最近一次变更的 DeepSeek {ANALYZE_MODEL} 分析已发送")
                )
            )
        else:
            client.send_message(msg.reply_with(ds(f"{target.label} 分析命令已结束，请查看日志")))

    threading.Thread(target=analyze, daemon=True).start()


def cmd_render_last(
    msg: "IcaNewMessage", client: "IcaClient", target_key: str | None = None
) -> None:
    global _last_render_cmd_at

    if target_key is not None:
        target = _target_configs.get(target_key)
        if target is None or not target.enabled:
            client.send_message(msg.reply_with(ds(f"未知或未启用监测目标: {target_key}（可选 chat、platform、docs）")))
            return

    if not is_admin(msg, client):
        client.send_message(msg.reply_with("只有管理员才能触发最近分析的主题图片渲染"))
        return

    if cooling_down(_last_render_cmd_at):
        return

    _last_render_cmd_at = time.monotonic()
    room_id = int(msg.room_id)
    target_label = target_key or "最近目标"
    client.send_message(
        msg.reply_with(ds(f"正在渲染 {target_label} 最近一次分析的深色、浅色、哀悼灰和喜庆红图片"))
    )

    def render_last() -> None:
        output = run_last_render(target_key, room_id)
        log(f"最近分析渲染结果: {output[:500]}")
        if "没有找到" in output:
            client.send_message(msg.reply_with(ds(output.strip()[:800])))
        elif "失败" in output or "❌" in output:
            client.send_message(msg.reply_with(ds(f"最近分析渲染失败:\n{output[:800]}")))
        elif "已发送" in output:
            client.send_message(msg.reply_with(ds("最近一次分析的四种主题图片已发送（一条消息，四张图）")))
        else:
            client.send_message(msg.reply_with(ds("最近分析渲染命令已结束，请查看日志")))

    threading.Thread(target=render_last, daemon=True).start()


def cmd_enable(msg: "IcaNewMessage", client: "IcaClient", on: bool) -> None:
    global _enabled
    _enabled = on
    if on:
        ok = start_watch()
        client.send_message(msg.reply_with(ds("监测已开启") if ok else ds("监测启动失败")))
    else:
        stop_watch()
        client.send_message(msg.reply_with(ds("监测已暂停")))


def cmd_help(msg: "IcaNewMessage", client: "IcaClient") -> None:
    client.send_message(
        msg.reply_with(
            "🔍 DS 监测：DeepSeek 网页监测\n"
            "/monitor         - 查看状态\n"
            "/monitor check   - 重启 watch 并立即检查，回报本轮结果（无变化则回“无事发生”）\n"
            "/monitor last    - 汇总 Chat、Platform、API Docs 最近修改\n"
            "/monitor last <chat|platform|docs> - 查看指定目标最近修改\n"
            "/monitor fp      - 查看最近 5 次指纹历史" + chr(10) + "/monitor fp <N>  - 查看最近 N 次指纹历史（1-20）" + chr(10) +
            "/monitor render last [chat|platform|docs] - 管理员发送最近分析四种主题图片\n"
            "/monitor analyze last <chat|platform|docs> - 管理员重新分析指定目标（无限时长）\n"
            "/monitor on/off  - 开关\n"
            "/monitor help    - 帮助"
        )
    )
