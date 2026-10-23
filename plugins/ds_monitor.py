"""
ds_monitor.py - DeepSeek 网页更新监测插件

启动 ds-monitor 二进制的 watch 模式监控 Chat、官网、Harness、Platform、API Docs 页面变更，
检测到变化时由 ds-monitor 自动通过 noticer 发送 AI 分析结果。配套 ds-monitor 0.5.0，
服务状态页基线与待发通知由 Rust 持久保存；Release、tag、commit、npm 与 profile
在独立快通道运行，不计入网页/文档周期状态字段。插件只负责进程管理、查询与看板发布。
配置房间通知走 /v1/send；命令触发的动态 room_id 通知走受 Token 保护的 direct API。

`/monitor check` 会重启 watch 并等待本轮的 `=== 本轮检查结果 ... ===` 标记
（ds-monitor 0.2.8 起输出），再回报各目标状态；本轮没有变化时回报“无事发生”。
`/monitor sp` 用一次只跑 status 的 `check` 现场查询状态页（不发通知，不影响 watch）。

整轮（含分析、通知）结束时 watch 还会输出 `=== 本轮完成 changes=… fingerprint=… analyzed=… ===`
（ds-monitor 0.4.1 起）。这一行排在分析之后，代表本轮的分析正文与图片都已落盘；
`publish = true` 时插件用它触发看板自动发布：跑 `deploy.ps1` 导出并部署 site/，
成功与失败都通过 noticer 通知到房间。一轮只会出现一次这个标记，所以一轮里多个目标
同时变化也只发布一次；发布期间又攒下的变化，会在这次发布结束后补一次。
`/monitor deploy` 可以手动发布一次（管理员）；有新增归档记录时回复对应深链接，否则回复主站。

配置 (config/ds_monitor.toml):

```toml
[ds_monitor]
binary = "D:\\githubs\\deepseek\\web_craw\\target\\release\\ds-monitor-copy.exe"
config = "D:\\githubs\\deepseek\\web_craw\\config.toml"
interval = 600
# 本轮有变化时自动导出并部署看板（Cloudflare Pages），默认关闭
publish = false
```

`binary` 指向 release 产物的**副本**：长跑的 watch 会锁住自己那个可执行文件，把构建
产物 `target\\release\\ds-monitor.exe` 留给 cargo，`cargo build --release` 就不会因为
监测在跑而失败。`/monitor update` 先让旧 watch 继续运行并构建、暂存新副本，成功后才
短暂停机原子替换；新进程启动失败会回滚旧副本。原先关闭的监测保持关闭，off/unload
会撤销排队启动；只有确认旧进程退出才允许替换，Windows 不允许覆盖正在运行的 exe。

`web_craw/config.toml` 保存 noticer strict/direct URL 与本地 Token；
仓库只提交不含密钥的 `config.example.toml`。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

from shenbot_api import PluginManifest, ConfigStorage

# 插件版本独立于 Rust 二进制版本；分析模型名集中供用户文案引用。
VERSION = "0.5.6"
ANALYZE_MODEL = "V41F"

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="ds_monitor",
    name="DeepSeek 网页更新监测",
    version=VERSION,
    description=(
        f"定期检查 DeepSeek Chat、官网、Harness、Platform 和 API Docs 变更，DeepSeek {ANALYZE_MODEL} 分析后推送通知；"
        "同时盯服务状态页的故障 / 恢复事件"
    ),
    authors=["shenjack"],
    config={
        "ds_monitor": ConfigStorage(
            binary="D:\\githubs\\deepseek\\web_craw\\target\\release\\ds-monitor-copy.exe",
            config="D:\\githubs\\deepseek\\web_craw\\config.toml",
            interval=600,
            publish=False,
        ),
    },
)

_binary_path: str = ""
_config_path: str = ""
_check_interval: int = 600
_enabled: bool = True
_target_configs: dict[str, "MonitorTarget"] = {}
_fingerprint_history_path: str = ""
# web_craw/config.toml 的 [status] 段（服务状态页订阅源），只用于展示与 /monitor sp
_status_config: dict[str, Any] = {}

_last_check_time: datetime | None = None
_last_change_time: datetime | None = None
_last_change_summary: str = ""
_last_check_times: dict[str, datetime] = {}
_last_change_times: dict[str, datetime] = {}
_last_change_summaries: dict[str, str] = {}
_active_output_target: str | None = None

_client: IcaClient | None = None
_watch_process: subprocess.Popen[str] | None = None
_watch_thread: threading.Thread | None = None
_watch_lock = threading.Lock()
# 锁序：生命周期锁 → 句柄锁；操作锁只保护占位，绝不持它等待生命周期锁或外部进程。
_lifecycle_lock = threading.RLock()
_operation_lock = threading.RLock()
# on/check/update 的排队任务携带意图代次；off/unload 递增它，使旧任务不能复活 watch。
_lifecycle_generation: int = 0
_unloading: bool = False
_watch_exited_generation: int = -1
_watch_stop_requested: bool = False
_watch_summary_lines: list[str] | None = None
# 正在收集的摘要在哪个目标名下（见 begin_summary/finish_summary）
_summary_owner: str | None = None
# 正在收集的 status 摘要是"基线快照"还是"状态变化"（决定归档到哪个变量）
_status_summary_baseline: bool = False
# 基线上就已存在的未恢复事件（watch 启动那一刻的状态），供 /monitor 展示
_last_status_snapshot: str = ""
# watch 进程代次：重启后加一，用于丢弃旧进程残留的输出行
_watch_generation: int = 0
_last_restart_cmd_at: float = 0.0
_last_analyze_cmd_at: float = 0.0
_last_render_cmd_at: float = 0.0
_last_status_page_cmd_at: float = 0.0
_last_update_cmd_at: float = 0.0
# 更新期间禁止再次触发：构建 + 换文件 + 重启 watch 不能并发
_update_in_progress: bool = False
_last_publish_cmd_at: float = 0.0
# 看板自动发布总闸（config/ds_monitor.toml 的 publish），默认关闭
_publish_enabled: bool = False
# 发布期间禁止并发触发：构建 + 导出 + 上传不能两路一起跑
_publish_in_progress: bool = False
# 发布进行中又攒下的待发布（新一轮有变化、或上一次没成功），本次结束后补一次
_publish_pending: bool = False
# 连续失败次数：连着失败到上限就停止自动重试，改由人工 /monitor deploy
_publish_failures: int = 0
_last_publish_at: float = 0.0
_last_publish_ok: bool | None = None
# 最近一轮（watch 的完成标记）"看板内容变化数"（网页/文档 + 指纹）与分析数，供 /monitor 与发布文案用
_last_cycle_changes: int = 0
_last_cycle_analyzed: int = 0

COMMAND_COOLDOWN_SECS = 60.0

# 构建产物的文件名：`binary` 配置的是它的副本（见模块文档）
SOURCE_BINARY_NAME = "ds-monitor.exe"
# `cargo build --release` 的最长等待时间（秒）：冷编译可能几分钟
BUILD_TIMEOUT_SECS = 900.0
# 看板部署脚本（导出 site/ 并上传 Cloudflare Pages），在 web_craw 仓库根目录
DEPLOY_SCRIPT_NAME = "deploy.ps1"
# Archive 的公开入口；具体记录使用前端支持的目录 + hash 深链接。
ARCHIVE_SITE_URL = "https://deepseek-monitor-archive.pages.dev"
# 一次发布（构建 + 导出 + 上传）的最长等待时间（秒）
PUBLISH_TIMEOUT_SECS = 1800.0
# 连着失败这么多次就停止自动重试，改成等人工 /monitor deploy
PUBLISH_MAX_FAILURES = 3

# ds-monitor 在检查阶段结束时输出的机器可读标记
# 形如：=== 本轮检查结果 changes=0 notices=0 errors=0 chat=nochange ... ===
CYCLE_RESULT_MARKER = "本轮检查结果"
# ds-monitor 整轮（含分析、通知）结束时输出的标记，作为看板自动发布的触发信号
# 形如：=== 本轮完成 changes=1 fingerprint=0 analyzed=1 ===
CYCLE_DONE_MARKER = "本轮完成"
# 等待本轮检查结果的最长时间（秒）：抓取 + 指纹探测通常远快于此
CYCLE_WAIT_SECS = 300.0

# 网页/文档目标的稳定顺序，同时用于状态、汇总、命令校验和帮助，避免新增目标时漏改一处。
WEB_TARGET_KEYS = ("chat", "official", "harness", "platform", "docs")
TARGET_OPTIONS = "、".join(WEB_TARGET_KEYS)
TARGET_HELP = "|".join(WEB_TARGET_KEYS)
CHANGE_PAGE_SIZE = 8

CYCLE_TARGET_LABELS = {
    "chat": "Chat",
    "official": "官网",
    "harness": "Harness",
    "platform": "Platform",
    "docs": "API Docs",
    "fingerprint": "系统指纹",
    "status": "服务状态页",
}
CYCLE_STATUS_LABELS = {
    "nochange": "无变化",
    "baseline": "首次抓取（已建基线）",
    "change": "检测到变化",
    "alert": "有告警",
    "error": "检查失败",
    "skipped": "本轮跳过",
}
# 基础错误目标；cycle_report 另纳入指纹失败，如实反映检查结论，不把快目标合成周期字段。
CYCLE_ERROR_KEYS = (*WEB_TARGET_KEYS, "status")

_cycle_event = threading.Event()
_cycle_result_generation: int = -1
_cycle_done_pending_generation: int = -1
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


def _as_bool(value: Any) -> bool:
    """把配置里可能是 bool 或字符串的值读成开关。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> None:
    global _binary_path, _config_path, _check_interval, _target_configs
    global _fingerprint_history_path, _status_config, _publish_enabled

    cfg = PLUGIN_MANIFEST.config_unchecked("ds_monitor")

    raw_bin = cfg.get_value("binary")
    _binary_path = str(raw_bin) if raw_bin else ""

    # 看板自动发布默认关闭：这是"对外发布"的动作，要人显式开闸
    _publish_enabled = _as_bool(cfg.get_value("publish"))

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
    _status_config = load_status_config()


def work_dir() -> str:
    # Rust 按当前工作目录解析配置中的 output/settings 等相对路径，因此运行目录取配置目录；
    # 构建仓库由 repo_root 单独定位，不能因配置外置而把运行目录改成 target/release。
    cwd = os.path.dirname(_config_path) if _config_path else ""
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.dirname(_binary_path) or "."
    return cwd


def source_binary_path() -> str:
    """构建产物路径：与 `binary` 同目录的 ds-monitor.exe。"""
    if not _binary_path:
        return ""
    return os.path.join(os.path.dirname(_binary_path), SOURCE_BINARY_NAME)


def repo_root() -> str:
    """定位构建仓库，不把外置配置的运行目录误当成 Cargo 工作目录。

    配置目录有可解析的 package/workspace 清单时优先使用，否则从二进制目录向上查找。
    不改变 work_dir 的配置相对路径规则；找不到清单就明确失败，不能回落到插件当前目录构建。
    """
    import tomllib

    def has_manifest(directory: str) -> bool:
        """仅认可可读取的 Cargo 包或工作区清单，错误不回显文件正文。"""
        try:
            with open(os.path.join(directory, "Cargo.toml"), "rb") as source:
                manifest = tomllib.load(source)
            return isinstance(manifest.get("package"), dict) or isinstance(manifest.get("workspace"), dict)
        except (OSError, ValueError):
            return False

    config_dir = os.path.dirname(os.path.abspath(_config_path)) if _config_path else ""
    if config_dir and has_manifest(config_dir):
        return config_dir
    directory = os.path.dirname(os.path.abspath(_binary_path)) if _binary_path else ""
    while directory:
        if has_manifest(directory):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    raise FileNotFoundError("找不到 ds-monitor 的有效 Cargo.toml 仓库目录")


def load_rust_config() -> dict[str, Any]:
    """读取 ds-monitor 的 config.toml；读不到时返回空 dict，由调用方各自用默认值兜底。"""
    if not _config_path or not os.path.isfile(_config_path):
        return {}
    try:
        import tomllib

        with open(_config_path, "rb") as f:
            raw = tomllib.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        log(f"读取 ds-monitor 配置失败（{type(exc).__name__}），显示层暂用默认值；启动前仍须校验")
        return {}


def load_target_configs() -> dict[str, MonitorTarget]:
    """读取 ds-monitor 的五类网页/文档目标，缺省值与 Rust 配置保持一致。"""
    raw = load_rust_config()

    sections = {
        "chat": ("Chat", raw.get("target", {}), True, "https://chat.deepseek.com/", "output/chat"),
        "official": (
            "官网",
            raw.get("official", {}),
            True,
            "https://www.deepseek.com/",
            "output/official",
        ),
        "harness": (
            "Harness",
            raw.get("harness", {}),
            True,
            "https://www.deepseek.com/harness/",
            "output/harness",
        ),
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
    section = load_rust_config().get("fingerprint", {})
    section = section if isinstance(section, dict) else {}
    history = str(section.get("history", "output/fingerprint-history.jsonl"))
    if not os.path.isabs(history):
        history = os.path.join(work_dir(), history)
    return history


def load_status_config() -> dict[str, Any]:
    """读取 Rust 侧 `[status]`（服务状态页监控）配置，缺省值与 `config.example.toml` 一致。"""
    section = load_rust_config().get("status", {})
    section = section if isinstance(section, dict) else {}
    try:
        interval = int(str(section.get("interval", 120)))
    except (TypeError, ValueError):
        interval = 120
    return {
        "enabled": bool(section.get("enabled", True)),
        "url": str(section.get("url", "https://status.deepseek.com/history.rss")),
        "fallback_url": str(
            section.get("fallback_url", "https://statuspage.flashduty.com/deepseek/history.rss")
        ),
        "interval": interval,
    }


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
    """启动前确认程序与显式配置可用，拒绝让 Rust 因缺文件创建默认模板后开始监控。

    仅只读检查配置的存在性和 TOML 语法；配置中的相对路径继续交给 work_dir 解释。
    解析错误只报告类型与中文诊断，不拼接可能带配置值的异常正文，也不修改配置文件。
    """
    if not _binary_path or not os.path.isfile(_binary_path):
        msg = f"❌ ds-monitor 二进制不存在: {_binary_path}"
        log(msg)
        notify_error(msg, room_id)
        return False
    if _config_path:
        if not os.path.isfile(_config_path):
            msg = "❌ ds-monitor 配置文件不存在或不是文件，已拒绝启动"
            log(msg)
            notify_error(msg, room_id)
            return False
        try:
            import tomllib

            with open(_config_path, "rb") as source:
                tomllib.load(source)
        except Exception as exc:
            msg = f"❌ ds-monitor 配置无法读取或 TOML 无效（{type(exc).__name__}），已拒绝启动"
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


# 只保留服务状态页的一次性 check；同时关闭五个独立监测，避免配置启用后抓外网、写缓存或发通知。
# 状态页可能尝试官方及镜像两个入口；关闭分析并清空通知出口，不把它当成完整监控轮次。
STATUS_ONLY_FLAGS = (
    "--no-chat",
    "--no-official",
    "--no-harness",
    "--no-platform",
    "--no-docs",
    "--no-fingerprint",
    "--no-harness-release",
    "--no-harness-tag",
    "--no-harness-commit",
    "--no-harness-npm",
    "--no-github-profile",
    "--no-analyze",
    "--noticer=",
)
STATUS_PROBE_TIMEOUT_SECS = 60.0
# 回复长度上限：一次故障可能同时有恢复 + 新故障，别把消息刷屏
REPLY_MAX_CHARS = 1200


def run_status_probe() -> str:
    """跑一次只含服务状态页的 `check`，用于 `/monitor sp` 现场查询。

    走的是 ds-monitor 自己的抓取链路（含官方域名失败后回退国际镜像），
    所以 Python 这边不需要也不应该自己去请求状态页。所有其他目标显式关闭，
    不读写 watch 缓存；超时、执行异常和非零退出均返回带失败标记的诊断，供回复层原样保留。
    """
    if not validate_binary():
        return f"❌ ds-monitor 启动前校验失败（二进制或配置不可用）: {_binary_path}"

    args = base_args("check")
    args.extend(STATUS_ONLY_FLAGS)

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_dir(),
            timeout=STATUS_PROBE_TIMEOUT_SECS,
        )
        out = result.stdout or ""
        if result.stderr:
            out += "\n" + result.stderr
        if result.returncode != 0:
            # 失败诊断优先于可能很长的进度 stdout，避免回复截断时丢掉真正错误。
            detail = "\n".join(part.strip() for part in (result.stderr, result.stdout) if part)
            return f"❌ 状态页查询失败（退出码 {result.returncode}）\n{detail}".strip()
        return out
    except subprocess.TimeoutExpired:
        return f"❌ 状态页查询超时（{int(STATUS_PROBE_TIMEOUT_SECS)}s）"
    except Exception as e:
        return f"❌ 状态页查询失败: {e}"


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
    else:
        # 服务状态页的进度行没有 `[标签]` 前缀，统一写成 `status: ...`
        # （见 web_craw/src/status/mod.rs），单独归属到 status 目标，
        # 免得它的报告被算进上一个网页目标的变更摘要里。
        if payload.startswith("status:"):
            _active_output_target = "status"

    target_key = _active_output_target or "chat"
    now = datetime.now(timezone.utc)

    if line.startswith(f"=== {CYCLE_RESULT_MARKER} "):
        # 记录本轮检查结果；继续走下面的汇总逻辑，让 "===" 正常结束变更摘要
        record_cycle_result(line, generation)

    if line.startswith(f"=== {CYCLE_DONE_MARKER} "):
        # 整轮（含分析、通知）结束：按本轮变化触发看板自动发布
        record_cycle_done(line, generation)

    if target_key == "status" and handle_status_output_line(payload, now):
        return

    if "检测到变化" in payload:
        _last_check_time = now
        _last_change_time = now
        _last_check_times[target_key] = now
        _last_change_times[target_key] = now
        begin_summary(target_key)
        return

    if "无变化" in payload or "首次抓取" in payload:
        _last_check_time = now
        _last_check_times[target_key] = now
        drop_summary(target_key)
        return

    finish_summary(target_key, payload)


def begin_summary(owner: str) -> None:
    """开始收集某个目标的变更摘要（覆盖上一份未完成的）。"""
    global _watch_summary_lines, _summary_owner

    _watch_summary_lines = []
    _summary_owner = owner


def drop_summary(owner: str) -> None:
    """丢弃某个目标未完成的摘要；owner 不匹配时不动别人的。"""
    global _watch_summary_lines, _summary_owner

    if _summary_owner == owner:
        _watch_summary_lines = None
        _summary_owner = None


def finish_summary(owner: str, payload: str) -> None:
    """按需结束摘要：遇 `===` / “已发送通知” 收尾，满 20 行提前收尾。

    收尾行（`=== 本轮检查结果 ...`）可能被记在别的目标名下——服务状态页是本轮最后一个
    目标，所以 `===` 到达时 owner 往往是 `status`——因此收尾一律以摘要真正的 owner 归档；
    只有内容行才要求 owner 匹配。
    """
    global _watch_summary_lines, _summary_owner, _last_status_snapshot
    global _last_change_summary

    lines = _watch_summary_lines
    ended = payload.startswith("===") or "已发送通知" in payload
    if ended:
        owner = _summary_owner or owner
    else:
        if _summary_owner != owner or lines is None:
            return
        stripped = payload.strip()
        if not stripped:
            return
        lines.append(stripped)
        if len(lines) < 20:
            return

    _watch_summary_lines = None
    _summary_owner = None
    if not lines:
        return

    text = "\n".join(lines)
    if owner == "status":
        # 基线上的未恢复事件只记"当前状态"，不算一次变更（/monitor sp 也是看这个）。
        if _status_summary_baseline:
            _last_status_snapshot = text
        else:
            _last_change_summary = text
            _last_change_summaries["status"] = text
        return
    _last_change_summary = text
    _last_change_summaries[owner] = text


def handle_status_output_line(payload: str, now: datetime) -> bool:
    """服务状态页输出的专门记账；返回 True 表示这一行已由本函数处理。

    认得的骨架（见 web_craw/src/status/mod.rs）：
    - `status: ...`：入口回退提示、首次检查基线、无变化、检测到状态变化；
    - `【DeepSeek 状态】...`：故障 / 状态推进 / 恢复的报告块（`---` 与空行为分隔）；
    - `=== 本轮检查结果 ... ===`：收尾（只在摘要确实属于 status 时才吞掉这一行）。
    """
    global _status_summary_baseline

    if payload.startswith("status:"):
        detail = payload[len("status:") :].strip()
        if "检测到状态变化" in detail:
            _last_check_times["status"] = now
            _last_change_times["status"] = now
            _status_summary_baseline = False
            begin_summary("status")
        elif "无变化" in detail:
            _last_check_times["status"] = now
            drop_summary("status")
        elif "首次检查" in detail or "基线" in detail:
            _last_check_times["status"] = now
            # 基线上若带着未恢复事件，Rust 会把明细一起打印出来，这里照收
            _status_summary_baseline = True
            begin_summary("status")
        return True

    if payload.startswith("【DeepSeek 状态】"):
        if _summary_owner != "status":
            begin_summary("status")
        if _watch_summary_lines is not None:
            _watch_summary_lines.append(payload.strip())
        return True

    if _summary_owner != "status":
        return False

    if payload.strip() == "---":
        return True
    finish_summary("status", payload)
    return True


def cycle_target_label(key: str) -> str:
    return CYCLE_TARGET_LABELS.get(key, key)


def cycle_status_text(key: str, status: str) -> str:
    return f"{cycle_target_label(key)} {CYCLE_STATUS_LABELS.get(status, status)}"


def parse_cycle_marker(
    line: str, marker: str, count_keys: tuple[str, ...]
) -> dict[str, str] | None:
    """仅接收完整机器行；非法数字、重复字段和截断行不影响 stdout 读取线程。"""
    prefix = f"=== {marker} "
    if not line.startswith(prefix) or not line.endswith(" ==="):
        return None
    fields: dict[str, str] = {}
    for token in line[len(prefix) : -4].split():
        key, sep, value = token.partition("=")
        if not sep or not key or not value or key in fields:
            return None
        fields[key] = value
    for key in count_keys:
        raw = fields.get(key, "")
        # isdigit 会接纳 int() 不支持的上标字符；先限制 ASCII 十进制与 u64 范围。
        if not (raw and len(raw) <= 20 and raw.isascii() and raw.isdecimal()):
            return None
        if int(raw) > (1 << 64) - 1:
            return None
    return fields


def record_cycle_result(line: str, generation: int) -> None:
    """保存当前进程代次的完整检查结果，广播唤醒等待者并开启一次完成标记窗口。"""
    global _cycle_result_generation, _cycle_statuses, _cycle_counts
    global _cycle_done_pending_generation
    count_keys = ("changes", "notices", "errors")
    fields = parse_cycle_marker(line, CYCLE_RESULT_MARKER, count_keys)
    if fields is None or any(
        fields.get(key) not in CYCLE_STATUS_LABELS for key in CYCLE_TARGET_LABELS
    ):
        return
    with _watch_lock:
        if generation != _watch_generation or _watch_stop_requested or _unloading:
            return
        _cycle_statuses = {key: fields[key] for key in CYCLE_TARGET_LABELS}
        _cycle_counts = {key: int(fields[key]) for key in count_keys}
        _cycle_result_generation = generation
        _cycle_done_pending_generation = generation
        _cycle_event.set()


def record_cycle_done(line: str, generation: int) -> None:
    """检查结果之后仅接收一次同代完成标记，再触发网页/文档或指纹的看板发布。

    严格结构与顺序排除普通日志、残留旧代和重复完成行；文本 stdout 仍不是独立事件通道，
    不能用于鉴别上游正文伪造的完整协议行。该边界在插件维护说明中记录。
    """
    global _last_cycle_changes, _last_cycle_analyzed, _cycle_done_pending_generation
    fields = parse_cycle_marker(
        line, CYCLE_DONE_MARKER, ("changes", "fingerprint", "analyzed")
    )
    if fields is None:
        return
    with _watch_lock:
        if (generation != _watch_generation or _watch_stop_requested or _unloading
                or _cycle_done_pending_generation != generation):
            return
        _cycle_done_pending_generation = -1
        changes = int(fields["changes"]) + int(fields["fingerprint"])
        _last_cycle_changes = changes
        _last_cycle_analyzed = int(fields["analyzed"])
    if _publish_enabled:
        maybe_publish(changes)


def wait_cycle_result(generation: int, timeout: float) -> bool:
    """等待指定进程代次的检查结果；进程提前退出、停用或被替换时立即结束等待。"""
    deadline = time.monotonic() + timeout
    while True:
        with _watch_lock:
            if generation != _watch_generation or _unloading or not _enabled:
                return False
            if _cycle_result_generation == generation:
                return True
            if (_watch_exited_generation == generation or _watch_process is None
                    or _watch_process.poll() is not None):
                return False
            _cycle_event.clear()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _cycle_event.wait(min(remaining, 0.2))


def cycle_report(elapsed: float) -> str:
    """把 Rust 周期状态标记整理成回复，任何已检查目标的失败都不能伪装成无事发生。

    指纹未配置时应由 Rust 返回跳过或失败；这里如实展示，不自行静音。
    独立快目标不在该状态行内，不合成它们的字段或把它们的诊断计入周期结果。
    """
    statuses = dict(_cycle_statuses)
    changes = [key for key, status in statuses.items() if status == "change"]
    alerts = [key for key, status in statuses.items() if status == "alert"]
    errors = [
        key for key in (*CYCLE_ERROR_KEYS, "fingerprint")
        if statuses.get(key) == "error"
    ]

    if changes:
        headline = "🔔 检测到变化：" + "、".join(cycle_target_label(key) for key in changes)
    elif alerts:
        headline = "⚠️ 本轮有告警：" + "、".join(cycle_target_label(key) for key in alerts)
    elif errors:
        headline = "⚠️ 本轮未发现变化"
    else:
        headline = "✅ 无事发生"

    if errors:
        headline += "；" + "、".join(cycle_target_label(key) for key in errors) + " 检查失败"
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
    """读取当前代输出；EOF 只终止等待，进程仍活着时保留句柄，避免失去停止能力。"""
    global _watch_process, _watch_exited_generation

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            handle_output_line(line, generation)
    except Exception as exc:
        log(f"watch 输出读取失败: {exc}")
    finally:
        code = proc.poll()
        # 不能拿生命周期锁：stop_watch 可能正持该锁等待本进程退出。
        with _watch_lock:
            if _watch_process is proc and generation == _watch_generation:
                if code is not None:
                    _watch_process = None
                _watch_exited_generation = generation
                _cycle_event.set()
                requested = _watch_stop_requested
            else:
                requested = True
        if not requested:
            log(f"watch 输出结束，exit={code}")


def start_watch(expected_generation: int | None = None) -> bool:
    """串行启动 watch；只执行仍有效的开启意图，立即退出或启动线程失败均返回 False。

    生命周期锁覆盖创建和登记，句柄锁只保护状态；off/unload 可在创建期间使意图失效。
    若创建后才发现取消，先登记再停止该进程，停止失败也保留句柄，绝不遗留失管进程。
    """
    global _watch_process, _watch_thread, _watch_stop_requested, _watch_generation

    with _lifecycle_lock:
        with _watch_lock:
            if (_unloading or not _enabled or (expected_generation is not None
                    and expected_generation != _lifecycle_generation)):
                return False
            if _watch_process is not None and _watch_process.poll() is None:
                return True
        if not validate_binary():
            return False
        args = base_args("watch")
        args.append(f"--interval={_check_interval}")
        try:
            proc = subprocess.Popen(
                args, cwd=work_dir(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            )
        except Exception as exc:
            log(f"watch 启动失败: {exc}")
            notify_error(f"❌ ds-monitor watch 启动失败: {exc}")
            return False
        with _watch_lock:
            _watch_process = proc
            _watch_stop_requested = False
            _watch_generation += 1
            generation = _watch_generation
            _cycle_event.clear()
            cancelled = (_unloading or not _enabled or (expected_generation is not None
                         and expected_generation != _lifecycle_generation))
        if cancelled:
            stop_watch()
            return False
        if proc.poll() is not None:
            with _watch_lock:
                if _watch_process is proc:
                    _watch_process = None
                _cycle_event.set()
            log(f"watch 启动后立即退出，exit={proc.returncode}")
            return False
        _watch_thread = threading.Thread(target=watch_stdout_loop, args=(proc, generation), daemon=True)
        try:
            _watch_thread.start()
        except Exception as exc:
            log(f"watch 输出线程启动失败: {exc}")
            stop_watch()
            return False
        if proc.poll() is not None:
            _cycle_event.set()
            return False
        log(f"watch 已启动 (pid={proc.pid}, interval={_check_interval}s, gen={generation})")
        return True


def stop_watch(timeout: float = 10.0) -> bool:
    """串行停止已登记进程；只有确认退出才清句柄，失败返回 False 并禁止后续替换。

    等待进程期间不持句柄锁，也不 join 输出线程，避免和 stdout 的 finally 互锁。
    本函数不改变开启意图，命令 off/unload 负责先使旧排队任务失效。
    """
    global _watch_process, _watch_stop_requested

    with _lifecycle_lock:
        with _watch_lock:
            proc = _watch_process
            _watch_stop_requested = True
            _cycle_event.set()
        if proc is None:
            return True
        if proc.poll() is None:
            log(f"停止 watch 进程 (pid={proc.pid})")
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log(f"watch 进程未按时退出，强制结束进程树 (pid={proc.pid})")
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=5, check=False,
                        )
                except Exception as exc:
                    log(f"结束 watch 进程树失败: {exc}")
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception as exc:
                    log(f"watch 强制停止失败: {exc}")
            except Exception as exc:
                log(f"watch 停止失败: {exc}")
        if proc.poll() is None:
            log(f"watch 仍未退出，保留进程句柄 (pid={proc.pid})")
            return False
        with _watch_lock:
            if _watch_process is proc:
                _watch_process = None
            _cycle_event.set()
        return True


def cooling_down(last_at: float) -> bool:
    return time.monotonic() - last_at < COMMAND_COOLDOWN_SECS


def is_admin(msg: IcaNewMessage, client: IcaClient) -> bool:
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
    return files


def change_page_lines(items: list[str], target_key: str, page: int) -> list[str]:
    total = len(items)
    pages = max(1, (total + CHANGE_PAGE_SIZE - 1) // CHANGE_PAGE_SIZE)
    if not 1 <= page <= pages:
        return [f"页码超出范围，共 {total} 项，可选页码 1-{pages}"]
    start = (page - 1) * CHANGE_PAGE_SIZE
    shown = items[start : start + CHANGE_PAGE_SIZE]
    lines = [f"共 {total} 项，第 {page}/{pages} 页，显示 {len(shown)} 项"]
    lines.extend(shown or ["暂无变更条目"])
    remaining = total - start - len(shown)
    if remaining:
        lines.append(f"后续还有 {remaining} 项，下一页：/monitor last {target_key} {page + 1}")
    if page > 1:
        lines.append(f"上一页：/monitor last {target_key} {page - 1}")
    return lines


def latest_web_change_report(target: MonitorTarget, page: int = 1) -> str:
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
            except (OSError, ValueError) as exc:
                return f"{target.label}: 读取最近变更 metadata 失败（{type(exc).__name__}）"
            if not isinstance(metadata, dict):
                return f"{target.label}: 最近变更 metadata 格式无效"

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
        lines.append("变更范围:")
        lines.extend(change_page_lines(
            [f"  - {file}" for file in changed_files], target.key, page
        ))
        if target.key in _last_change_summaries:
            lines.append(f"运行摘要:\n{_last_change_summaries[target.key]}")
        lines.append(f"目录: {name}")
        return "\n".join(lines)

    return f"{target.label}: 还没有记录到带 diff 的历史网页修改"


def latest_docs_change_report(target: MonitorTarget, page: int = 1) -> str:
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
        except (OSError, ValueError) as exc:
            return f"{target.label}: 读取 manifest 失败（{type(exc).__name__}）"

        if not isinstance(manifest, dict):
            return f"{target.label}: manifest 格式无效"
        entries = manifest.get("changes")
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            return f"{target.label}: manifest 变更清单格式无效"
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
        items = [
            f"  - [{entry.get('kind', 'unknown')}] {entry.get('title') or entry.get('url', 'N/A')}"
            for entry in entries
        ]
        if len(entries) == 1 and entries[0].get("url"):
            items[0] += f"\n链接: {entries[0]['url']}"
        lines.extend(change_page_lines(items, target.key, page))
        lines.append(f"清单: {os.path.relpath(manifest_path, target.output)}")
        return "\n".join(lines)
    return f"{target.label}: 还没有历史变更 manifest"


def latest_change_report(target_key: str | None = None, page: int = 1) -> str:
    targets = _target_configs
    if target_key is not None:
        target = targets.get(target_key)
        if target is None:
            return ds(f"未知监测目标: {target_key}（可选 {TARGET_OPTIONS}）")
        if not target.enabled:
            return ds(f"{target.label} 监测未启用")
        reports = [
            latest_docs_change_report(target, page)
            if target.key == "docs"
            else latest_web_change_report(target, page)
        ]
    else:
        reports = []
        for key in WEB_TARGET_KEYS:
            target = targets.get(key)
            if target is not None and target.enabled:
                reports.append(
                    latest_docs_change_report(target, page)
                    if target.key == "docs"
                    else latest_web_change_report(target, page)
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
    except (OSError, UnicodeError) as exc:
        return ds(f"读取 fingerprint 历史失败（{type(exc).__name__}）")

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
        errors = record.get("errors")
        if not isinstance(errors, list) or any(not isinstance(error, str) for error in errors):
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


def status_page_report(output: str) -> str:
    """把一次 status-only `check` 的 stdout 整理成一条可读回复。

    认得的骨架（见 web_craw/src/status/mod.rs）：
    - `status: <摘要>`：入口回退提示、基线/无变化计数；
    - `【DeepSeek 状态】...` 块：未恢复事件的明细（每块以空行或 `---` 分隔）；
    - `=== 本轮检查结果 ... status=baseline|nochange|change|error ===`：本轮结论。

    执行失败优先于进程打印过的成功片段；跳过和缺失完整标记不等于健康，
    仅可信成功结论且没有故障明细时显示无未恢复事件。这里只格式化，不抓取、不发送。
    """
    if "unexpected argument" in output:
        return ds(
            "❌ ds-monitor 二进制不认识状态页查询参数，说明跑的还是旧版本；"
            "先重新构建 release 再试\n" + output.strip()[:600]
        )

    if output.lstrip().startswith("❌"):
        return ds(output.strip()[:REPLY_MAX_CHARS])

    status_field = ""
    summary = ""
    error = ""
    mirror = False
    blocks: list[str] = []
    current: list[str] = []

    for raw in output.splitlines():
        line = raw.strip()
        if not line or line == "---":
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if line.startswith("status:"):
            detail = line[len("status:") :].strip()
            if "镜像入口" in detail:
                mirror = True
            else:
                summary = detail
            continue
        if line.startswith("===") and CYCLE_RESULT_MARKER in line:
            if current:
                blocks.append("\n".join(current))
                current = []
            for token in line.split():
                key, sep, value = token.partition("=")
                if sep and key == "status":
                    status_field = value
            continue
        if line.startswith("【DeepSeek 状态】"):
            if current:
                blocks.append("\n".join(current))
            current = [line]
            continue
        if current:
            current.append(line)
            continue
        if not error and ("状态页检查失败" in line or "状态页抓取失败" in line):
            error = line
    if current:
        blocks.append("\n".join(current))

    lines = ["服务状态页查询"]
    if summary:
        lines.append(summary)
    if mirror:
        lines.append("入口: 官方域名不可用，已自动回退国际镜像")
    failed = status_field == "error" or bool(error)
    if failed:
        lines.append("❌ " + (error or "订阅源抓不到，本轮没拿到状态"))
    elif status_field == "skipped":
        lines.append("⚠️ 服务状态页检查已跳过，请确认状态页监测是否启用")
    elif status_field not in {"baseline", "nochange", "change", "alert"}:
        lines.append("❌ 未返回完整的状态页检查结果，无法确认当前服务状态")
    if blocks:
        lines.append("")
        for block in blocks[:3]:
            lines.append(block)
            lines.append("")
        if len(blocks) > 3:
            lines.append(f"（另有 {len(blocks) - 3} 条未恢复事件）")
    elif status_field in {"baseline", "nochange"} and not failed:
        lines.append("当前没有未恢复事件 ✅")

    return ds("\n".join(lines).strip()[:REPLY_MAX_CHARS])


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
    """加载配置并建立新意图代次；卸载前排队任务不能作用于这一代。"""
    global _unloading, _lifecycle_generation
    with _watch_lock:
        _unloading = False
        _lifecycle_generation += 1
        intent = _lifecycle_generation
    try:
        load_config()
    except Exception as exc:
        log(f"加载配置失败，改用内置默认值继续启动: {type(exc).__name__}: {exc}")
    enabled = ", ".join(target.label for target in _target_configs.values() if target.enabled)
    status_state = "启用" if _status_config.get("enabled", True) else "未启用"
    log(f"加载完成 (binary={_binary_path}, interval={_check_interval}s, targets={enabled}, status={status_state})")
    if _enabled:
        start_watch(intent)


def on_unload() -> None:
    """先撤销排队启动，再停止当前句柄；后台构建结束也不能复活进程。"""
    global _unloading, _lifecycle_generation
    with _watch_lock:
        _unloading = True
        _lifecycle_generation += 1
        _cycle_event.set()
    if not stop_watch():
        log("卸载时 watch 未能停止，句柄已保留，禁止后续启动")
    log("卸载")


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
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
    elif content == "/monitor update":
        cmd_update(msg, client)
    elif content == "/monitor deploy":
        cmd_deploy(msg, client)
    elif content == "/monitor sp":
        cmd_status_page(msg, client)
    elif content == "/monitor fp":
        cmd_fingerprint(msg, client)
    elif len(parts) == 3 and parts[:2] == ["/monitor", "fp"]:
        cmd_fingerprint(msg, client, parts[2])
    elif len(parts) in (3, 4) and parts[:2] == ["/monitor", "last"]:
        cmd_last(msg, client, parts[2], parts[3] if len(parts) == 4 else None)
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


def cmd_status(msg: IcaNewMessage, client: IcaClient) -> None:
    """分别展示开启意图与实际进程，停止失败不能显示为已经暂停。"""
    with _watch_lock:
        running = _watch_process is not None and _watch_process.poll() is None
        enabled = _enabled
    if running:
        state = "✅ 运行中" if enabled else "⚠️ 已请求暂停，但进程尚未退出"
    else:
        state = "⚠️ 已开启但进程未运行" if enabled else "⏸ 已暂停"
    lines = [
        "🔍 DS 监测：DeepSeek 网页监测",
        f"插件版本: {PLUGIN_MANIFEST.version}",
        f"EXE版本: {binary_version()}",
        f"状态: {state}",
        f"检查间隔: {_check_interval}s",
    ]
    if _publish_enabled:
        if _publish_in_progress:
            publish_state = "⏳ 正在发布"
        elif _publish_failures >= PUBLISH_MAX_FAILURES:
            publish_state = "⏸ 连续失败已熔断（/monitor deploy 手动恢复）"
        elif _publish_pending:
            publish_state = "🕓 待发布（下一轮补）"
        elif _last_publish_ok is None:
            publish_state = "等待本轮变化"
        elif _last_publish_ok:
            publish_state = "✅ 上次成功"
        else:
            publish_state = f"❌ 上次失败（连续 {_publish_failures} 次）"
        publish_line = f"看板自动发布: {publish_state}"
        if _last_publish_at:
            at = datetime.fromtimestamp(_last_publish_at, timezone.utc)
            publish_line += f" · {at.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        lines.append(publish_line)
    else:
        lines.append("看板自动发布: ⏸ 未开启（publish = false；/monitor deploy 可手动发布）")
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
    for key in WEB_TARGET_KEYS:
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

    status_cfg = _status_config
    status_state = "✅ 启用" if status_cfg.get("enabled", True) else "⏸ 未启用"
    lines.append(f"服务状态页: {status_state}")
    lines.append(f"  订阅源: {status_cfg.get('url', 'N/A')}")
    if status_cfg.get("fallback_url"):
        lines.append(f"  备用入口: {status_cfg['fallback_url']}")
    lines.append(
        f"  抓取间隔: {status_cfg.get('interval', 120)}s"
        "（watch 持久保存基线与待发通知；/monitor sp 只查询、不写状态）"
    )
    checked = _last_check_times.get("status")
    changed = _last_change_times.get("status")
    if checked:
        lines.append(f"  上次检查: {checked.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if changed:
        lines.append(f"  上次变更: {changed.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    status_summary = _last_change_summaries.get("status")
    if status_summary:
        lines.append(f"  最近状态变化:\n{status_summary[:600]}")
    if _last_status_snapshot:
        lines.append(f"  启动时未恢复事件:\n{_last_status_snapshot[:600]}")

    client.send_message(msg.reply_with("\n".join(lines)))


def _lifecycle_reply(msg: IcaNewMessage, client: IcaClient, text: str) -> None:
    """生命周期回复失败只记日志，不能撤销已受理任务或打断回滚。调用时不持状态锁。"""
    try:
        client.send_message(msg.reply_with(text))
    except Exception as exc:
        log(f"生命周期命令回复失败: {type(exc).__name__}: {exc}")


def cmd_check(msg: IcaNewMessage, client: IcaClient) -> None:
    """提交可取消的重启检查；更新期间不插队，等待结果不持生命周期锁。"""
    global _enabled, _last_restart_cmd_at, _lifecycle_generation
    rejection = ""
    with _operation_lock:
        if _update_in_progress or _unloading:
            rejection = "正在更新或卸载监测，暂不能重启检查"
        elif cooling_down(_last_restart_cmd_at):
            rejection = "命令冷却中，请稍后再试"
        else:
            _last_restart_cmd_at = time.monotonic()
            with _watch_lock:
                _enabled = True
                _lifecycle_generation += 1
                intent = _lifecycle_generation
    if rejection:
        _lifecycle_reply(msg, client, ds(rejection))
        return
    _lifecycle_reply(msg, client, ds("正在重启 watch，启动后会立即检查并回报本轮结果"))

    def restart() -> None:
        with _lifecycle_lock:
            with _operation_lock:
                if _update_in_progress:
                    return
            with _watch_lock:
                if _unloading or not _enabled or intent != _lifecycle_generation:
                    return
            if not stop_watch():
                _lifecycle_reply(msg, client, ds("旧 watch 未能停止，未启动新进程"))
                return
            if not start_watch(intent):
                with _watch_lock:
                    if intent != _lifecycle_generation or _unloading or not _enabled:
                        return
                _lifecycle_reply(msg, client, ds("watch 重启失败"))
                return
            generation = _watch_generation
        started = time.monotonic()
        ready = wait_cycle_result(generation, CYCLE_WAIT_SECS)
        with _watch_lock:
            if intent != _lifecycle_generation or _unloading or not _enabled:
                return
            alive = _watch_process is not None and _watch_process.poll() is None
        if ready:
            report = cycle_report(time.monotonic() - started)
        elif not alive or _watch_exited_generation == generation:
            report = ds("watch 已提前退出或输出中断，未取得本轮检查结果，请查看日志")
        else:
            report = ds(f"watch 已重启，但 {int(CYCLE_WAIT_SECS)}s 内未取得本轮检查结果")
        _lifecycle_reply(msg, client, report)

    threading.Thread(target=restart, daemon=True).start()


def noticer_rooms(cfg: dict[str, Any]) -> list[str]:
    """noticer 目标房间：主房间在前，再补 rooms 里的广播房间，去重且丢掉空白。

    与 ds-monitor 的 `NoticerConfig::broadcast_rooms` 同一套语义，免得同类通知在两边
    发到不同房间。
    """
    names: list[str] = []
    primary = str(cfg.get("room") or "").strip()
    if primary:
        names.append(primary)
    extra = cfg.get("rooms") or []
    if isinstance(extra, str):
        extra = [extra]
    for item in extra:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
    return names


def noticer_post(text: str) -> bool:
    """向所有配置房间发送发布结论，HTTP 与业务确认均成功才算成功。

    只检查本次通知，不因通知失败重跑已经完成的部署；Token 仅放请求头。
    单房间失败不阻断其他广播房间，也不把部分广播成功误报为全部成功。
    """
    cfg = load_rust_config().get("noticer") or {}
    url = str(cfg.get("url") or "").strip()
    rooms = noticer_rooms(cfg)
    if not url or not rooms:
        log("未配置 noticer 的房间或 URL，跳过发布通知")
        return False

    token = str(cfg.get("token") or "")
    all_sent = True
    for room in rooms:
        try:
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(
                url,
                data=json.dumps({"room": room, "message": text}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                # noticer 的确认是小型 JSON；拒绝异常大正文，避免无界读取。
                body = response.read(65537)
                if len(body) > 65536:
                    raise ValueError("确认正文超限")
                payload = json.loads(body)
                if not (200 <= response.status < 300):
                    raise ValueError("HTTP 状态未成功")
                if not isinstance(payload, dict) or payload.get("status") != "ok":
                    raise ValueError("业务状态未确认成功")
        except Exception as exc:
            all_sent = False
            # 不回显服务端正文或异常中的 URL，避免把私有配置带进日志。
            log(f"发布通知到 {room} 未确认成功（{type(exc).__name__}）")
    return all_sent


def site_stats(root: str) -> tuple[int, float]:
    """site/ 的文件数与体积（MiB），用于发布回报。"""
    count = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(os.path.join(root, "site")):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
            count += 1
    return count, total / (1024 * 1024)


def publish_url(output: str) -> str:
    """从 deploy.ps1 的输出里取生产域名（脚本最后一行会打印）。"""
    marker = "生产域名："
    index = output.rfind(marker)
    if index < 0:
        return ""
    return output[index + len(marker) :].strip().split(" ")[0]


def archive_entries(root: str) -> list[tuple[str, str]] | None:
    """读取当前公开索引中的记录标识；索引不存在或损坏时返回 None。

    返回顺序沿用导出器顺序。部署前后都成功读取时才能判定新增记录，避免首次部署把全部
    历史条目误报成新页面。
    """
    path = os.path.join(root, "site", "data", "site.json")
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError, TypeError):
        return None

    # 两类集合都由导出器固定生成；缺一类不能补成空集合，否则会把历史误报为新增。
    if not isinstance(data, dict) or not {"targets", "fingerprints"}.issubset(data):
        return None
    targets = data["targets"]
    fingerprints = data["fingerprints"]
    if not isinstance(targets, dict) or not isinstance(fingerprints, list):
        return None
    entries: list[tuple[str, str]] = []
    for target, items in targets.items():
        if not isinstance(target, str) or not isinstance(items, list):
            return None
        for item in items:
            entry_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(entry_id, str) or not entry_id:
                return None
            entries.append((target, entry_id))
    for item in fingerprints:
        entry_id = item.get("checked_at") if isinstance(item, dict) else None
        if not isinstance(entry_id, str) or not entry_id:
            return None
        entries.append(("fingerprints", entry_id))
    return entries


def new_archive_urls(
    before: list[tuple[str, str]] | None,
    after: list[tuple[str, str]] | None,
    base_url: str,
) -> list[str]:
    """生成部署中新出现记录的公开深链接；无法可靠比较时不返回链接。"""
    if before is None or after is None:
        return []
    old = set(before)
    root = base_url.rstrip("/")
    return [
        f"{root}/{quote(target, safe='')}/#/{quote(target, safe='')}/{quote(entry_id, safe='')}"
        for target, entry_id in after
        if (target, entry_id) not in old
    ]


def reserve_publish(automatic: bool) -> bool:
    """在启动线程前原子占位，与 update 共用操作锁。

    自动发布受开关和连续失败熔断控制；人工发布可用于恢复熔断。
    更新或发布正在进行时只保留待办，不并发构建或导出。锁内不做外部调用。
    """
    global _publish_in_progress, _publish_pending
    with _operation_lock:
        if _unloading:
            return False
        if automatic and (
            not _publish_enabled or _publish_failures >= PUBLISH_MAX_FAILURES
        ):
            return False
        if _update_in_progress or _publish_in_progress:
            _publish_pending = True
            return False
        _publish_in_progress = True
        _publish_pending = False
        return True


def maybe_publish(changes: int) -> None:
    """合并变化并安排一次发布；连续失败到上限后等人工恢复。

    占位发生在线程启动前，下一轮变化只置 pending；启动线程失败则还回待办。
    """
    global _publish_pending, _publish_in_progress
    with _operation_lock:
        if _unloading or not _publish_enabled:
            return
        if changes > 0:
            _publish_pending = True
        if not _publish_pending or not reserve_publish(automatic=True):
            return
    try:
        threading.Thread(
            target=publish_once, args=(True,), kwargs={"reserved": True}, daemon=True
        ).start()
    except Exception as exc:
        with _operation_lock:
            _publish_in_progress = False
            _publish_pending = True
        log(f"发布线程启动失败（{type(exc).__name__}），保留待发布")


def publish_once(notify_room: bool, *, reserved: bool = False) -> str:
    """执行已占位的发布，失败统一记账，通知失败不重跑部署。

    自动与手工共用同一事务出口；缺脚本、异常、超时都纳入连续失败计数。
    发布期间新增的待办在成功后合并补一次，失败则留给下一轮，达到上限即熔断。
    插件卸载后不启动排队的发布；已经运行的部署不在此处强杀。
    """
    global _publish_in_progress, _publish_pending, _publish_failures
    global _last_publish_at, _last_publish_ok

    if not reserved and not reserve_publish(automatic=notify_room):
        return ds("发布暂未执行：正在更新/发布、自动重试已暂停，或插件正在卸载")
    with _operation_lock:
        if _unloading:
            _publish_in_progress = False
            _publish_pending = True
            return ds("插件正在卸载，已取消排队的看板发布")

    started = time.monotonic()
    ok = False
    report = ""
    try:
        root = repo_root()
        script = os.path.join(root, DEPLOY_SCRIPT_NAME)
        if not os.path.isfile(script):
            raise FileNotFoundError("找不到部署脚本")
        log(f"开始发布看板: {script}")
        entries_before = archive_entries(root)
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PUBLISH_TIMEOUT_SECS,
        )
        output = (result.stdout or "") + (result.stderr or "")
        ok = result.returncode == 0 and "Deployment complete!" in output
        if ok:
            count, size = site_stats(root)
            url = publish_url(output) or ARCHIVE_SITE_URL
            fresh_urls = new_archive_urls(entries_before, archive_entries(root), url)
            lines = ["✅ 看板已更新"]
            if fresh_urls:
                lines.append("新页面：")
                # 避免一次积压生成无界通知；完整记录仍可从主站访问。
                lines.extend(fresh_urls[:8])
                if len(fresh_urls) > 8:
                    lines.append(f"另有 {len(fresh_urls) - 8} 条记录：{url}")
            else:
                lines.append(url)
            lines.append(
                f"site/ 共 {count} 个文件、{size:.1f} MiB，"
                f"用时 {time.monotonic() - started:.0f}s"
            )
            report = ds("\n".join(lines))
        else:
            report = ds(
                f"❌ 看板发布失败（exit {result.returncode}）:\n{output.strip()[-500:]}"
            )
    except subprocess.TimeoutExpired:
        report = ds(f"❌ 看板发布超时（{int(PUBLISH_TIMEOUT_SECS)}s），本次放弃")
    except Exception as exc:
        report = ds(f"❌ 看板发布失败: {type(exc).__name__}: {exc}")
    finally:
        with _operation_lock:
            _last_publish_at = time.time()
            _last_publish_ok = ok
            if ok:
                _publish_failures = 0
            else:
                _publish_failures += 1
                _publish_pending = True
                if _publish_failures >= PUBLISH_MAX_FAILURES:
                    report += (
                        f"\n（已连续失败 {_publish_failures} 次，暂停自动重试；"
                        "修好后用 /monitor deploy 手动发布）"
                    )
        # 通知仍在占位内，防止新一轮在旧任务通知期间挤入并覆盖发布状态。
        try:
            if notify_room and not noticer_post(report):
                log("发布结论未全部送达；部署结果已记录，不因此重复部署")
        except Exception as exc:
            log(f"发布结论通知异常（{type(exc).__name__}），不重复部署")
        finally:
            with _operation_lock:
                _publish_in_progress = False

    if ok:
        maybe_publish(0)
    return report


def cmd_deploy(msg: IcaNewMessage, client: IcaClient) -> None:
    """管理员手动发布；原子占位可绕过熔断，结果只回复当前命令。"""
    global _last_publish_cmd_at, _publish_in_progress, _publish_pending
    if not is_admin(msg, client):
        client.send_message(msg.reply_with(ds("只有管理员才能手动发布看板")))
        return

    with _operation_lock:
        if cooling_down(_last_publish_cmd_at):
            report = ds("命令冷却中，请稍后再试")
            accepted = False
        else:
            accepted = reserve_publish(automatic=False)
            report = ds(
                "正在导出并部署看板（构建 + 导出 + 上传，通常 1-3 分钟）"
                if accepted else "正在更新/发布，或插件正在卸载，请稍后再试"
            )
            if accepted:
                _last_publish_cmd_at = time.monotonic()
    if not accepted:
        client.send_message(msg.reply_with(report))
        return

    def deploy() -> None:
        result = publish_once(False, reserved=True)
        try:
            client.send_message(msg.reply_with(result))
        except Exception as exc:
            log(f"手动发布结果回复失败（{type(exc).__name__}）")

    try:
        try:
            client.send_message(msg.reply_with(report))
        except Exception as exc:
            log(f"发布开始回复失败（{type(exc).__name__}），继续执行已受理任务")
        threading.Thread(target=deploy, daemon=True).start()
    except Exception as exc:
        with _operation_lock:
            _publish_in_progress = False
            _publish_pending = True
        log(f"手动发布线程启动失败（{type(exc).__name__}），已释放占位")


def cmd_update(msg: IcaNewMessage, client: IcaClient) -> None:
    """先构建并暂存新副本，成功后才短暂停机原子替换；新进程启动失败则回滚旧副本。

    操作锁仅负责与发布互斥占位；构建、通知、等待结果都不持锁。生命周期锁只覆盖
    最终停止/替换/启动，off/unload 可随时撤销意图。原先关闭时只更新文件，不启动。
    """
    global _last_update_cmd_at, _update_in_progress, _lifecycle_generation
    if not is_admin(msg, client):
        _lifecycle_reply(msg, client, ds("只有管理员才能触发更新"))
        return
    source = source_binary_path()
    destination = _binary_path
    if not destination or not source:
        _lifecycle_reply(msg, client, ds("未配置 ds-monitor 二进制路径，无法更新"))
        return
    if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(destination)):
        _lifecycle_reply(msg, client, ds("binary 指向构建产物本体，请改成同目录运行副本"))
        return
    # 与已有切换先串行，再原子占位；不能持操作锁等待生命周期锁。
    rejection = ""
    with _lifecycle_lock:
        with _operation_lock:
            if _update_in_progress or _publish_in_progress:
                rejection = "已有更新或看板发布在进行中，请稍候"
            elif cooling_down(_last_update_cmd_at):
                rejection = "命令冷却中，请稍后再试"
            else:
                with _watch_lock:
                    if _unloading:
                        rejection = "插件正在卸载，已取消更新"
                    else:
                        _lifecycle_generation += 1
                        intent = _lifecycle_generation
                if not rejection:
                    _last_update_cmd_at = time.monotonic()
                    _update_in_progress = True
    if rejection:
        _lifecycle_reply(msg, client, ds(rejection))
        return

    def current() -> bool:
        with _watch_lock:
            return not _unloading and intent == _lifecycle_generation

    def watch_state() -> str:
        with _watch_lock:
            alive = _watch_process is not None and _watch_process.poll() is None
            return "旧 watch 仍在运行" if alive else "当前未运行 watch，未自动开启"

    def reply(text: str) -> None:
        _lifecycle_reply(msg, client, ds(text))

    def update() -> None:
        global _update_in_progress
        staging = ""
        keep_backup = False
        generation: int | None = None
        try:
            if not current():
                return
            try:
                result = subprocess.run(
                    ["cargo", "build", "--release", "--bin", "ds-monitor"],
                    cwd=repo_root() or None, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=BUILD_TIMEOUT_SECS,
                )
                if result.returncode != 0:
                    tail = (result.stderr or result.stdout or "").strip()[-500:]
                    reply(f"构建失败（exit {result.returncode}）:\n{tail}\n{watch_state()}")
                    return
            except subprocess.TimeoutExpired:
                reply(f"构建超时（{int(BUILD_TIMEOUT_SECS)}s）\n{watch_state()}")
                return
            except Exception as exc:
                reply(f"构建失败: {exc}\n{watch_state()}")
                return
            if not current():
                reply("更新的后续切换已取消，未重启 watch")
                return
            # 同一磁盘暂存并备份，任一次复制失败都不触碰运行中的旧文件。
            try:
                staging = tempfile.mkdtemp(prefix=".ds-monitor-update-", dir=os.path.dirname(destination) or ".")
                staged = os.path.join(staging, "new.exe")
                backup = os.path.join(staging, "old.exe")
                shutil.copy2(source, staged)
                had_backup = os.path.isfile(destination)
                if had_backup:
                    shutil.copy2(destination, backup)
            except Exception as exc:
                reply(f"暂存副本失败，未替换旧文件: {exc}\n{watch_state()}")
                return
            def rollback(reason: str) -> None:
                """在生命周期锁内恢复旧文件；取消不复活，停止或替换失败保留可手动恢复的备份。"""
                nonlocal keep_backup
                with _lifecycle_lock:
                    if not current() or not _enabled:
                        reply(f"{reason}；开启意图已取消，未额外恢复监测")
                        return
                    if not had_backup:
                        reply(f"{reason}；原先没有旧副本可回滚，请查看日志")
                        return
                    if not stop_watch():
                        keep_backup = True
                        reply(f"{reason}；新 watch 未能停止，禁止回滚覆盖；旧副本保留在 {backup}")
                        return
                    try:
                        os.replace(backup, destination)
                    except Exception as exc:
                        keep_backup = True
                        reply(f"{reason}；回滚失败: {exc}；旧副本保留在 {backup}")
                        return
                    restored = current() and _enabled and start_watch(intent)
                    state = "已恢复旧副本监测" if restored else "旧副本已恢复到磁盘，但监测未启动"
                    reply(f"{reason}，已回滚旧文件；{state}")

            with _lifecycle_lock:
                if not current():
                    reply("更新的后续切换已取消，未重启 watch")
                    return
                if not stop_watch():
                    reply("旧 watch 未能停止，保留句柄，未替换运行副本")
                    return
                try:
                    os.replace(staged, destination)
                except Exception as exc:
                    restored = current() and _enabled and start_watch(intent)
                    state = "已用未改动的旧副本恢复监测" if restored else watch_state()
                    reply(f"原子替换失败，旧文件未改动: {exc}\n{state}")
                    return
                if not current() or not _enabled:
                    reply("副本更新完成；监测保持暂停或已卸载，未启动 watch")
                    return
                if not start_watch(intent):
                    rollback("新副本启动失败")
                    return
                generation = _watch_generation
            started = time.monotonic()
            ready = wait_cycle_result(generation, CYCLE_WAIT_SECS)
            if not current() or not _enabled:
                reply("副本已更新；监测已暂停或被后续操作取代，未额外恢复")
                return
            if ready:
                report = cycle_report(time.monotonic() - started)
            elif _watch_exited_generation == generation or _watch_process is None or _watch_process.poll() is not None:
                rollback("新 watch 在首轮结果前退出或输出中断")
                return
            else:
                report = "watch 已启动，但未在限定时间内取得本轮检查结果"
            reply(f"更新完成\n\n{report}")
        finally:
            if staging and not keep_backup:
                try:
                    shutil.rmtree(staging)
                except OSError as exc:
                    log(f"清理更新临时目录失败: {exc}")
            with _operation_lock:
                _update_in_progress = False

    # 回复、线程构造或 start 失败，都必须归还预占，不能让更新永久卡在进行中。
    try:
        reply("正在更新：旧 watch 继续运行 → 构建 release → 暂存副本 → 短暂停机原子切换")
        threading.Thread(target=update, daemon=True).start()
    except Exception as exc:
        with _operation_lock:
            _update_in_progress = False
        log(f"无法启动更新任务: {exc}")


def cmd_status_page(msg: IcaNewMessage, client: IcaClient) -> None:
    """`/monitor sp`：现场查询服务状态页，不重启 watch、不发通知。"""
    global _last_status_page_cmd_at

    if cooling_down(_last_status_page_cmd_at):
        remain = int(COMMAND_COOLDOWN_SECS - (time.monotonic() - _last_status_page_cmd_at)) + 1
        client.send_message(msg.reply_with(ds(f"命令冷却中，请 {remain} 秒后再试")))
        return

    _last_status_page_cmd_at = time.monotonic()

    def probe() -> None:
        output = run_status_probe()
        log(f"状态页查询结果: {output[:500]}")
        client.send_message(msg.reply_with(status_page_report(output)))

    threading.Thread(target=probe, daemon=True).start()


def cmd_fingerprint(
    msg: IcaNewMessage, client: IcaClient, raw_limit: str | None = None
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
    msg: IcaNewMessage,
    client: IcaClient,
    target_key: str | None = None,
    page_text: str | None = None,
) -> None:
    try:
        page = int(page_text) if page_text is not None else 1
        if page < 1:
            raise ValueError("页码应为正整数")
    except ValueError:
        client.send_message(msg.reply_with(ds("页码必须是正整数，用法：/monitor last <目标> [页码]")))
        return
    # 仅读本地摘要，允许连续翻页；联网和重分析命令仍沿用各自冷却。
    client.send_message(msg.reply_with(latest_change_report(target_key, page)))


def cmd_last_analyze(
    msg: IcaNewMessage, client: IcaClient, target_key: str
) -> None:
    global _last_analyze_cmd_at

    target = _target_configs.get(target_key)
    if target is None or not target.enabled:
        client.send_message(msg.reply_with(ds(f"未知或未启用监测目标: {target_key}（可选 {TARGET_OPTIONS}）")))
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
    msg: IcaNewMessage, client: IcaClient, target_key: str | None = None
) -> None:
    global _last_render_cmd_at

    if target_key is not None:
        target = _target_configs.get(target_key)
        if target is None or not target.enabled:
            client.send_message(msg.reply_with(ds(f"未知或未启用监测目标: {target_key}（可选 {TARGET_OPTIONS}）")))
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


def cmd_enable(msg: IcaNewMessage, client: IcaClient, on: bool) -> None:
    """开启提交可取消任务；暂停先撤销全部旧意图，停止失败保留句柄并如实报告。"""
    global _enabled, _lifecycle_generation
    if not on:
        if not is_admin(msg, client):
            _lifecycle_reply(msg, client, ds("只有管理员才能暂停监测"))
            return
        with _watch_lock:
            _enabled = False
            _lifecycle_generation += 1
            intent = _lifecycle_generation
            _cycle_event.set()
        with _lifecycle_lock:
            with _watch_lock:
                superseded = intent != _lifecycle_generation
            stopped = superseded or stop_watch()
        text = "暂停请求已被新的开启命令取代" if superseded else (
            "监测已暂停" if stopped else "已取消后续启动，但 watch 未能停止；保留句柄，请检查日志")
        _lifecycle_reply(msg, client, ds(text))
        return
    rejection = ""
    with _operation_lock:
        if _update_in_progress or _unloading:
            rejection = "正在更新或卸载监测，暂不能开启"
        else:
            with _watch_lock:
                _enabled = True
                _lifecycle_generation += 1
                intent = _lifecycle_generation
    if rejection:
        _lifecycle_reply(msg, client, ds(rejection))
        return

    def enable() -> None:
        with _lifecycle_lock:
            with _operation_lock:
                if _update_in_progress:
                    return
            with _watch_lock:
                if _unloading or not _enabled or intent != _lifecycle_generation:
                    return
                running = _watch_process is not None and _watch_process.poll() is None
            if running:
                _lifecycle_reply(msg, client, ds("监测已在运行（要立即检查用 /monitor check）"))
                return
            if not start_watch(intent):
                with _watch_lock:
                    if _unloading or not _enabled or intent != _lifecycle_generation:
                        return
                _lifecycle_reply(msg, client, ds("监测启动失败"))
                return
            generation = _watch_generation
        started = time.monotonic()
        ready = wait_cycle_result(generation, CYCLE_WAIT_SECS)
        with _watch_lock:
            if _unloading or not _enabled or intent != _lifecycle_generation:
                return
            alive = _watch_process is not None and _watch_process.poll() is None
        if ready:
            report = cycle_report(time.monotonic() - started)
        elif not alive or _watch_exited_generation == generation:
            report = ds("watch 已提前退出或输出中断，未取得本轮检查结果，请查看日志")
        else:
            report = ds(f"没在 {int(CYCLE_WAIT_SECS)}s 内取得本轮检查结果，稍后用 /monitor 查看")
        _lifecycle_reply(msg, client, f"{ds('监测已开启')}\n\n{report}")

    threading.Thread(target=enable, daemon=True).start()


def cmd_help(msg: IcaNewMessage, client: IcaClient) -> None:
    client.send_message(
        msg.reply_with(
            "🔍 DS 监测：DeepSeek 网页监测\n"
            "/monitor         - 查看状态\n"
            "/monitor check   - 重启 watch 并立即检查，回报本轮结果（无变化则回“无事发生”）\n"
            "/monitor sp      - 现场查询服务状态页（故障/恢复事件，不发通知）\n"
            "/monitor update  - 管理员构建 release、刷新运行副本并重启 watch\n"
            "/monitor deploy  - 管理员立即导出并部署看板（Cloudflare Pages）\n"
            "/monitor last    - 汇总 Chat、官网、Harness、Platform、API Docs 最近修改\n"
            f"/monitor last <{TARGET_HELP}> [页码] - 查看最近修改，每页 8 项\n"
            "/monitor fp      - 查看最近 5 次指纹历史" + chr(10) + "/monitor fp <N>  - 查看最近 N 次指纹历史（1-20）" + chr(10) +
            f"/monitor render last [{TARGET_HELP}] - 发送最近分析四种主题图片\n"
            f"/monitor analyze last <{TARGET_HELP}> - 管理员重新分析指定目标（无限时长）\n"
            "/monitor on/off  - 开关（on 谁都可以，会回报本轮检查结果；off 需管理员）\n"
            "/monitor help    - 帮助"
        )
    )
